"""Tests for the execution engine module."""

import copy
import json
import pytest
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime

from src.data.trade_journal import _TABLE_SCHEMA
from src.strategy.execution_engine import (
    DROP_REASONS, ExecutionEngine, PositionsUnavailable)
from src.strategy.put_seller import PutSeller
from src.strategy.call_seller import CallSeller
from src.utils.config import Config

TRADE_SCHEMA_FIELD_NAMES = {field.name for field in _TABLE_SCHEMA}


class TestFilterDuplicateOpportunities:
    """Test ExecutionEngine.filter_duplicate_opportunities."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_alpaca = Mock()
        self.mock_config = Mock(spec=Config)
        self.engine = ExecutionEngine(self.mock_alpaca, self.mock_config)

    def test_removes_existing_positions(self):
        """Test that opportunities matching existing positions are removed."""
        opportunities = [
            {'option_symbol': 'AAPL250117P00170000', 'symbol': 'AAPL'},
            {'option_symbol': 'MSFT250117P00380000', 'symbol': 'MSFT'},
            {'option_symbol': 'GOOGL250117P00150000', 'symbol': 'GOOGL'},
        ]
        existing_positions = [
            {'symbol': 'AAPL250117P00170000'},
            {'symbol': 'GOOGL250117P00150000'},
        ]

        filtered, count = self.engine.filter_duplicate_opportunities(
            opportunities, existing_positions
        )

        assert len(filtered) == 1
        assert filtered[0]['symbol'] == 'MSFT'
        assert count == 2

    def test_keeps_all_when_no_existing_positions(self):
        """Test all opportunities kept when no existing positions."""
        opportunities = [
            {'option_symbol': 'AAPL250117P00170000', 'symbol': 'AAPL'},
            {'option_symbol': 'MSFT250117P00380000', 'symbol': 'MSFT'},
        ]

        filtered, count = self.engine.filter_duplicate_opportunities(
            opportunities, []
        )

        assert len(filtered) == 2
        assert count == 0

    def test_empty_opportunities(self):
        """Test with empty opportunity list."""
        filtered, count = self.engine.filter_duplicate_opportunities([], [])

        assert filtered == []
        assert count == 0

    def test_all_duplicates(self):
        """Test when all opportunities are duplicates."""
        opportunities = [
            {'option_symbol': 'AAPL250117P00170000', 'symbol': 'AAPL'},
        ]
        existing_positions = [
            {'symbol': 'AAPL250117P00170000'},
        ]

        filtered, count = self.engine.filter_duplicate_opportunities(
            opportunities, existing_positions
        )

        assert filtered == []
        assert count == 1


class TestRankOpportunities:
    """Test ExecutionEngine.rank_opportunities."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_alpaca = Mock()
        self.mock_config = Mock(spec=Config)
        self.mock_config.max_position_size = 0.10
        self.engine = ExecutionEngine(self.mock_alpaca, self.mock_config)

        self.mock_put_seller = Mock(spec=PutSeller)

    def test_sorts_by_roi_descending(self):
        """Test that opportunities are sorted by ROI highest first."""
        opportunities = [
            {'symbol': 'AAPL', 'strike_price': 170.0, 'premium': 2.50, 'option_symbol': 'AAPL250117P00170000'},
            {'symbol': 'MSFT', 'strike_price': 380.0, 'premium': 5.00, 'option_symbol': 'MSFT250117P00380000'},
        ]

        # AAPL: collateral=17000, premium=250, roi=250/17000=0.0147
        # MSFT: collateral=38000, premium=500, roi=500/38000=0.0132
        self.mock_put_seller._calculate_position_size.return_value = {
            'contracts': 1,
        }

        ranked = self.engine.rank_opportunities(
            opportunities, self.mock_put_seller, 50000.0
        )

        assert len(ranked) == 2
        assert ranked[0]['roi'] >= ranked[1]['roi']

    def test_skips_opportunities_that_fail_sizing(self):
        """Test that opportunities failing position sizing are excluded."""
        opportunities = [
            {'symbol': 'AAPL', 'strike_price': 170.0, 'premium': 2.50, 'option_symbol': 'AAPL250117P00170000'},
            {'symbol': 'MSFT', 'strike_price': 380.0, 'premium': 5.00, 'option_symbol': 'MSFT250117P00380000'},
        ]

        # First succeeds, second fails sizing
        self.mock_put_seller._calculate_position_size.side_effect = [
            {'contracts': 1},
            None,
        ]

        ranked = self.engine.rank_opportunities(
            opportunities, self.mock_put_seller, 50000.0
        )

        assert len(ranked) == 1
        assert ranked[0]['opportunity']['symbol'] == 'AAPL'

    def test_empty_opportunities(self):
        """Test with empty opportunity list."""
        ranked = self.engine.rank_opportunities(
            [], self.mock_put_seller, 50000.0
        )
        assert ranked == []

    def test_adds_mid_price_from_premium(self):
        """Test that premium is copied to mid_price for position sizing."""
        opportunities = [
            {'symbol': 'AAPL', 'strike_price': 100.0, 'premium': 1.50, 'option_symbol': 'AAPL250117P00170000'},
        ]

        self.mock_put_seller._calculate_position_size.return_value = {
            'contracts': 1,
        }

        ranked = self.engine.rank_opportunities(
            opportunities, self.mock_put_seller, 50000.0
        )

        assert len(ranked) == 1
        # Verify mid_price was set on the opportunity
        assert opportunities[0]['mid_price'] == 1.50


class TestSelectBatch:
    """Test ExecutionEngine.select_batch."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_alpaca = Mock()
        self.mock_config = Mock(spec=Config)
        self.engine = ExecutionEngine(self.mock_alpaca, self.mock_config)

    def test_respects_buying_power_limit(self):
        """Test that batch selection stops when buying power exhausted."""
        ranked = [
            {
                'opportunity': {'symbol': 'AAPL', 'option_symbol': 'AAPL250117P00170000'},
                'collateral': 17000.0,
                'premium': 250.0,
                'roi': 0.015,
            },
            {
                'opportunity': {'symbol': 'MSFT', 'option_symbol': 'MSFT250117P00380000'},
                'collateral': 38000.0,
                'premium': 500.0,
                'roi': 0.013,
            },
        ]

        # Only 20000 buying power - can afford AAPL but not MSFT
        selected, remaining_bp = self.engine.select_batch(ranked, 20000.0)

        assert len(selected) == 1
        assert selected[0]['symbol'] == 'AAPL'
        assert remaining_bp == 3000.0  # 20000 - 17000

    def test_enforces_one_position_per_underlying(self):
        """Test that only one position per underlying is selected."""
        ranked = [
            {
                'opportunity': {'symbol': 'AAPL', 'option_symbol': 'A1'},
                'collateral': 17000.0,
                'premium': 300.0,
                'roi': 0.018,
            },
            {
                'opportunity': {'symbol': 'AAPL', 'option_symbol': 'A2'},
                'collateral': 16000.0,
                'premium': 250.0,
                'roi': 0.016,
            },
            {
                'opportunity': {'symbol': 'MSFT', 'option_symbol': 'B1'},
                'collateral': 38000.0,
                'premium': 500.0,
                'roi': 0.013,
            },
        ]

        selected, remaining_bp = self.engine.select_batch(ranked, 100000.0)

        # Should pick first AAPL and MSFT, skip second AAPL
        assert len(selected) == 2
        symbols = [s['symbol'] for s in selected]
        assert symbols.count('AAPL') == 1
        assert 'MSFT' in symbols

    def test_empty_ranked_list(self):
        """Test with no ranked opportunities."""
        selected, remaining_bp = self.engine.select_batch([], 50000.0)

        assert selected == []
        assert remaining_bp == 50000.0

    def test_no_affordable_opportunities(self):
        """Test when no opportunities fit within buying power."""
        ranked = [
            {
                'opportunity': {'symbol': 'AAPL', 'option_symbol': 'AAPL250117P00170000'},
                'collateral': 17000.0,
                'premium': 250.0,
                'roi': 0.015,
            },
        ]

        selected, remaining_bp = self.engine.select_batch(ranked, 5000.0)

        assert selected == []
        assert remaining_bp == 5000.0

    def test_selects_multiple_underlyings(self):
        """Test selecting opportunities across different underlyings."""
        ranked = [
            {
                'opportunity': {'symbol': 'AAPL', 'option_symbol': 'AAPL250117P00170000'},
                'collateral': 10000.0,
                'premium': 200.0,
                'roi': 0.020,
            },
            {
                'opportunity': {'symbol': 'MSFT', 'option_symbol': 'MSFT250117P00380000'},
                'collateral': 10000.0,
                'premium': 180.0,
                'roi': 0.018,
            },
            {
                'opportunity': {'symbol': 'GOOGL', 'option_symbol': 'GOOGL250117P00150000'},
                'collateral': 10000.0,
                'premium': 150.0,
                'roi': 0.015,
            },
        ]

        selected, remaining_bp = self.engine.select_batch(ranked, 25000.0)

        assert len(selected) == 2  # Can afford 2 out of 3
        assert remaining_bp == 5000.0


class TestExecuteBatch:
    """Test ExecutionEngine.execute_batch."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_alpaca = Mock()
        self.mock_config = Mock(spec=Config)
        self.engine = ExecutionEngine(self.mock_alpaca, self.mock_config)

        self.mock_put_seller = Mock(spec=PutSeller)

    def test_successful_batch_execution(self):
        """Test executing a batch of orders successfully."""
        self.mock_put_seller.execute_put_sale.return_value = {
            'success': True,
            'order_id': 'order-123',
        }

        opportunities = [
            {'symbol': 'AAPL', 'option_symbol': 'AAPL250117P00170000', 'contracts': 1, 'premium': 2.5, 'strike_price': 170},
            {'symbol': 'MSFT', 'option_symbol': 'MSFT250117P00380000', 'contracts': 1, 'premium': 5.0, 'strike_price': 380},
        ]

        results, trades_count = self.engine.execute_batch(
            opportunities, self.mock_put_seller
        )

        assert len(results) == 2
        assert trades_count == 2
        assert all(r['success'] for r in results)

    def test_handles_order_failure_gracefully(self):
        """Test that one order failure does not stop the batch."""
        self.mock_put_seller.execute_put_sale.side_effect = [
            {'success': True, 'order_id': 'order-1'},
            {'success': False, 'message': 'Insufficient margin'},
            {'success': True, 'order_id': 'order-3'},
        ]

        opportunities = [
            {'symbol': 'AAPL', 'option_symbol': 'AAPL250117P00170000', 'contracts': 1, 'premium': 2.5, 'strike_price': 170},
            {'symbol': 'MSFT', 'option_symbol': 'MSFT250117P00380000', 'contracts': 1, 'premium': 5.0, 'strike_price': 380},
            {'symbol': 'GOOGL', 'option_symbol': 'GOOGL250117P00150000', 'contracts': 1, 'premium': 3.0, 'strike_price': 150},
        ]

        results, trades_count = self.engine.execute_batch(
            opportunities, self.mock_put_seller
        )

        assert len(results) == 3
        assert trades_count == 2  # 2 out of 3 succeeded
        assert results[0]['success'] is True
        assert results[1]['success'] is False
        assert results[2]['success'] is True

    def test_handles_exception_during_execution(self):
        """Test that exceptions during order execution are caught."""
        self.mock_put_seller.execute_put_sale.side_effect = [
            {'success': True, 'order_id': 'order-1'},
            Exception("Network timeout"),
        ]

        opportunities = [
            {'symbol': 'AAPL', 'option_symbol': 'AAPL250117P00170000', 'contracts': 1, 'premium': 2.5, 'strike_price': 170},
            {'symbol': 'MSFT', 'option_symbol': 'MSFT250117P00380000', 'contracts': 1, 'premium': 5.0, 'strike_price': 380},
        ]

        results, trades_count = self.engine.execute_batch(
            opportunities, self.mock_put_seller
        )

        assert len(results) == 2
        assert trades_count == 1
        assert results[0]['success'] is True
        assert results[1]['success'] is False
        assert 'Network timeout' in results[1]['result']['message']

    def test_empty_batch(self):
        """Test executing an empty batch."""
        results, trades_count = self.engine.execute_batch(
            [], self.mock_put_seller
        )

        assert results == []
        assert trades_count == 0
        self.mock_put_seller.execute_put_sale.assert_not_called()

    def test_all_orders_fail(self):
        """Test batch where all orders fail."""
        self.mock_put_seller.execute_put_sale.return_value = {
            'success': False,
            'message': 'Market closed',
        }

        opportunities = [
            {'symbol': 'AAPL', 'option_symbol': 'AAPL250117P00170000', 'contracts': 1, 'premium': 2.5, 'strike_price': 170},
            {'symbol': 'MSFT', 'option_symbol': 'MSFT250117P00380000', 'contracts': 1, 'premium': 5.0, 'strike_price': 380},
        ]

        results, trades_count = self.engine.execute_batch(
            opportunities, self.mock_put_seller
        )

        assert len(results) == 2
        assert trades_count == 0
        assert all(not r['success'] for r in results)

    def test_passes_skip_buying_power_check_false(self):
        """Test that execute_batch calls put_seller with skip_buying_power_check=False."""
        self.mock_put_seller.execute_put_sale.return_value = {
            'success': True,
            'order_id': 'order-1',
        }

        opportunities = [
            {'symbol': 'AAPL', 'option_symbol': 'AAPL250117P00170000', 'contracts': 1, 'premium': 2.5, 'strike_price': 170},
        ]

        self.engine.execute_batch(opportunities, self.mock_put_seller)

        self.mock_put_seller.execute_put_sale.assert_called_once_with(
            opportunities[0], skip_buying_power_check=False
        )


PUT_SYM = "AAPL250117P00170000"
CALL_SYM = "AAPL250117C00190000"


class TestExecuteBatchRouting:
    """FC-048: routing is derived from the OCC symbol, not from a dict key.

    The bug: `opp.get('type', 'put')`. Only the scanner sets 'type'; the sellers
    set 'strategy'. So every seller-produced covered call defaulted to "put",
    went to put_seller, and was rejected — which is why every backtest modelled
    a put-only wheel while looking healthy.
    """

    def setup_method(self):
        from src.strategy.execution_engine import clear_failed_symbols
        clear_failed_symbols()      # module-global; must not leak between tests
        self.alpaca = Mock()
        # Enough shares for the covered-call path's availability check.
        self.alpaca.get_positions.return_value = [
            {"symbol": "AAPL", "qty": "500", "asset_class": "us_equity", "side": "long"}
        ]
        self.engine = ExecutionEngine(self.alpaca, Mock(spec=Config))
        self.put_seller = Mock(spec=PutSeller)
        self.call_seller = Mock(spec=CallSeller)
        self.put_seller.execute_put_sale.return_value = {"success": True, "order_id": "p1"}
        self.call_seller.execute_call_sale.return_value = {"success": True, "order_id": "c1"}

    def _run(self, opp):
        return self.engine.execute_batch([opp], self.put_seller, call_seller=self.call_seller)

    def test_seller_shaped_call_without_type_key_routes_to_call(self):
        """THE FC-048 REGRESSION. Fails on the old code: defaults to 'put'."""
        self._run({"symbol": "AAPL", "option_symbol": CALL_SYM, "strategy": "sell_call",
                   "contracts": 1, "premium": 1.0, "strike_price": 190,
                   "shares_covered": 100, "stock_cost_basis": 150.0})

        self.call_seller.execute_call_sale.assert_called_once()
        self.put_seller.execute_put_sale.assert_not_called()

    def test_scanner_shaped_call_still_routes_to_call(self):
        """Production /run executes scanner-shaped dicts — must not change."""
        self._run({"symbol": "AAPL", "option_symbol": CALL_SYM, "type": "call",
                   "contracts": 1, "premium": 1.0, "strike_price": 190,
                   "shares_covered": 100, "stock_cost_basis": 150.0})

        self.call_seller.execute_call_sale.assert_called_once()
        self.put_seller.execute_put_sale.assert_not_called()

    def test_scanner_shaped_put_routes_to_put_with_bp_check(self):
        self._run({"symbol": "AAPL", "option_symbol": PUT_SYM, "type": "put",
                   "contracts": 1, "premium": 2.5, "strike_price": 170})

        self.put_seller.execute_put_sale.assert_called_once()
        assert self.put_seller.execute_put_sale.call_args.kwargs[
            "skip_buying_power_check"] is False
        self.call_seller.execute_call_sale.assert_not_called()

    def test_seller_shaped_put_routes_to_put(self):
        self._run({"symbol": "AAPL", "option_symbol": PUT_SYM, "strategy": "sell_put",
                   "contracts": 1, "premium": 2.5, "strike_price": 170})

        self.put_seller.execute_put_sale.assert_called_once()
        self.call_seller.execute_call_sale.assert_not_called()

    def test_unroutable_symbol_fails_loud_and_trades_nothing(self):
        """The silent-default class: a missing/garbage symbol must NOT trade.

        The bare-ticker cases are the sharp ones. parse_option_symbol's
        heuristic resolves 'AAPL' -> "put" and 'NOT_AN_OCC' -> "call"; routing
        on that would send a non-contract to a seller, and place_option_order
        on a bare ticker is a plain EQUITY order. Adjusted roots ('1AAPL...')
        are refused too — their deliverable is not 100 shares.
        """
        for bad in ({"symbol": "AAPL", "contracts": 1},                       # no option_symbol
                    {"symbol": "AAPL", "option_symbol": "", "contracts": 1},
                    {"symbol": "AAPL", "option_symbol": "NOT_AN_OCC", "contracts": 1},
                    {"symbol": "AAPL", "option_symbol": "AAPL", "contracts": 1},
                    {"symbol": "SPY", "option_symbol": "SPY", "contracts": 1},
                    {"symbol": "AAPL", "option_symbol": "1AAPL250117C00190000",
                     "contracts": 1}):
            self.put_seller.reset_mock(); self.call_seller.reset_mock()
            results, count = self._run(bad)

            assert count == 0
            assert results[0]["success"] is False
            assert results[0]["result"]["error_type"] == "unroutable_opportunity"
            assert results[0]["result"]["non_retryable"] is True
            self.put_seller.execute_put_sale.assert_not_called()
            self.call_seller.execute_call_sale.assert_not_called()

    def test_unroutable_opportunity_suppresses_the_retry_storm(self):
        """Plan D1 promised non_retryable feeds _failed_symbols; it must."""
        from src.strategy.execution_engine import (
            clear_failed_symbols, get_failed_symbols)

        clear_failed_symbols()
        try:
            self._run({"symbol": "AAPL", "option_symbol": "NOT_AN_OCC", "contracts": 1})
            # Keyed on the contract, not the underlying: blacklisting "AAPL"
            # would suppress every future legitimate AAPL contract.
            assert "NOT_AN_OCC" in get_failed_symbols()
            assert "AAPL" not in get_failed_symbols()
        finally:
            clear_failed_symbols()

    def test_one_unroutable_opportunity_does_not_kill_the_batch(self):
        results, count = self.engine.execute_batch(
            [{"symbol": "AAPL", "option_symbol": "", "contracts": 1},
             {"symbol": "AAPL", "option_symbol": PUT_SYM, "strategy": "sell_put",
              "contracts": 1, "premium": 2.5, "strike_price": 170}],
            self.put_seller, call_seller=self.call_seller)

        assert len(results) == 2 and count == 1
        self.put_seller.execute_put_sale.assert_called_once()

    def test_contradictory_type_key_loses_to_the_occ_symbol(self):
        """The contract is what place_option_order actually trades."""
        self.engine.logger = Mock()
        self._run({"symbol": "AAPL", "option_symbol": CALL_SYM, "type": "put",
                   "contracts": 1, "premium": 1.0, "strike_price": 190,
                   "shares_covered": 100, "stock_cost_basis": 150.0})

        self.call_seller.execute_call_sale.assert_called_once()
        self.put_seller.execute_put_sale.assert_not_called()
        assert any(
            c.kwargs.get("event_type") == "opportunity_type_mismatch"
            for c in self.engine.logger.warning.call_args_list
        ), "the type/symbol contradiction must be logged, not silently resolved"


class TestProducerVocabulary:
    """The producer must declare its own type (FC-048 D2).

    The sellers setting only 'strategy' while the scanner set only 'type' is
    the vocabulary asymmetry that caused the covered-call misroute. FC-068
    deleted the seller-side producers (``evaluate_covered_call_opportunity``,
    ``find_put_opportunity``), so ``OptionsScanner`` is the sole remaining one
    — and the contract is *restated against it*, not dropped: an opportunity
    with no declared type is exactly what the misroute was made of.

    Stated for the record: the scanner sets ``type`` and no ``strategy`` key.
    ``ExecutionEngine._declared_type`` still understands both vocabularies and
    is untouched — the OCC symbol remains the router; ``type`` is the
    telemetry/mismatch-warning input.
    """

    def _scanner(self):
        from src.data.options_scanner import OptionsScanner

        config = Mock(spec=Config)
        config.put_target_dte = 7
        config.call_target_dte = 7
        market_data = Mock()
        market_data.get_stock_metrics.return_value = {"current_price": 160.05}
        return OptionsScanner(Mock(), market_data, config)

    def test_scanner_call_opportunity_declares_its_type(self):
        """Assert the REAL emitted dict, not the source text."""
        scanner = self._scanner()

        opp = scanner._create_call_opportunity(
            {"symbol": "AAPL250117C00190000", "strike_price": 190.0,
             "expiration_date": "2025-01-17", "dte": 7, "delta": 0.20,
             "mid_price": 1.10},
            {"symbol": "AAPL", "qty": "100"},
            150.0,
        )

        assert opp is not None, (
            "builder returned None — the stub is too loose; this test must "
            "ASSERT, never skip"
        )
        assert opp["type"] == "call"
        assert ExecutionEngine._declared_type(opp) == "call"

    def test_scanner_put_opportunity_declares_its_type(self):
        scanner = self._scanner()

        opp = scanner._create_put_opportunity(
            {"symbol": "AAPL250117P00150000", "strike_price": 150.0,
             "expiration_date": "2025-01-17", "dte": 7, "delta": -0.20,
             "mid_price": 1.10},
            {"symbol": "AAPL", "current_price": 160.05},
        )

        assert opp is not None
        assert opp["type"] == "put"
        assert ExecutionEngine._declared_type(opp) == "put"

    def test_the_producer_declares_type_in_its_opportunity_shape(self):
        """Belt-and-braces contract check that survives a constant refactor.

        Repointed by FC-068 from the two seller modules — whose only ``'type'``
        literals lived inside the deleted producers — to the sole survivor.
        """
        from pathlib import Path as _P

        body = _P("src/data/options_scanner.py").read_text()
        for want in ("'type': 'put'", "'type': 'call'"):
            assert want in body, f"options_scanner.py no longer declares {want}"


class TestCommittedSharesCheck:
    """FC-052: the oversell guard must count only real short CALLS.

    It previously did `'C' in opt_sym` over a hand-rolled underlying, so a
    short PUT on any ticker containing a "C" counted as a committed call —
    over-reporting committed shares and starving the call side. Fifth instance
    of the OCC-substring family (FC-041/043/045/048).
    """

    def _engine_with(self, positions):
        alpaca = Mock()
        alpaca.get_positions.return_value = positions
        return ExecutionEngine(alpaca, Mock(spec=Config)), alpaca

    def _run_call(self, engine, call_seller):
        return engine.execute_batch(
            [{"symbol": "CVX", "option_symbol": "CVX250117C00160000",
              "strategy": "sell_call", "contracts": 1, "premium": 1.0,
              "strike_price": 160, "shares_covered": 100,
              "stock_cost_basis": 150.0}],
            Mock(spec=PutSeller), call_seller=call_seller)

    def test_a_short_put_on_a_c_ticker_does_not_consume_call_capacity(self):
        """The bug: CVX...P... contains 'C', so it counted as a committed call."""
        call_seller = Mock(spec=CallSeller)
        call_seller.execute_call_sale.return_value = {"success": True, "order_id": "c1"}
        engine, _ = self._engine_with([
            {"symbol": "CVX", "qty": "100", "asset_class": "us_equity", "side": "long"},
            # A short PUT on the same underlying. Not a call; must not commit shares.
            {"symbol": "CVX250117P00140000", "qty": "-1", "asset_class": "us_option"},
        ])

        self._run_call(engine, call_seller)

        call_seller.execute_call_sale.assert_called_once(), (
            "a short PUT consumed the shares backing a legitimate covered call"
        )

    def test_a_real_short_call_still_consumes_capacity(self):
        """The guard must keep working: don't over-correct into overselling."""
        call_seller = Mock(spec=CallSeller)
        call_seller.execute_call_sale.return_value = {"success": True, "order_id": "c1"}
        engine, _ = self._engine_with([
            {"symbol": "CVX", "qty": "100", "asset_class": "us_equity", "side": "long"},
            # 100 shares already backing a short CALL -> nothing available.
            {"symbol": "CVX250117C00170000", "qty": "-1", "asset_class": "us_option"},
        ])

        self._run_call(engine, call_seller)

        call_seller.execute_call_sale.assert_not_called()


# ======================================================================
# FC-038: two-pool execution selection
#
# Covered calls were charged strike x 100 of cash collateral they never
# needed -- at BOTH the sizing stage (put_seller._calculate_position_size's
# buying-power cap) and the selection stage. One call could therefore exhaust
# the cash budget for the whole batch, and AAPL went four trading days without
# a covered call while its calls were the top-scored opportunities in every
# scan. See docs/investigations/covered-call-starvation-2026-07-18.md.
# ======================================================================

FIXTURES = Path(__file__).parent / "fixtures"

# Buying power logged by the production /run cycle being replayed.
GOLDEN_BP = 67142.50


def _drop_events(mock_logger):
    """Every `selection_dropped` payload emitted on a mocked engine logger."""
    return [c.kwargs for c in mock_logger.info.call_args_list
            if c.kwargs.get("event_type") == "selection_dropped"]


def _select_events(mock_logger):
    return [c.kwargs for c in mock_logger.info.call_args_list
            if c.kwargs.get("event_type") == "opportunity_selected"]


def _call(symbol, strike, *, expiry="260724", premium=1.0, score=None, **extra):
    opp = {
        "symbol": symbol,
        "option_symbol": f"{symbol}{expiry}C{int(strike * 1000):08d}",
        "type": "call",
        "strike_price": float(strike),
        "premium": premium,
    }
    if score is not None:
        opp["attractiveness_score"] = score
    opp.update(extra)
    return opp


def _put(symbol, strike, *, expiry="260724", premium=1.0, **extra):
    opp = {
        "symbol": symbol,
        "option_symbol": f"{symbol}{expiry}P{int(strike * 1000):08d}",
        "type": "put",
        "strike_price": float(strike),
        "premium": premium,
    }
    opp.update(extra)
    return opp


def _equity(symbol, qty):
    return {"symbol": symbol, "qty": str(qty), "asset_class": "us_equity",
            "side": "long"}


def _short_option(option_symbol, qty=-1):
    return {"symbol": option_symbol, "qty": str(qty), "asset_class": "us_option",
            "side": "short"}


class TestAvailableSharesHelper:
    """The committed-shares arithmetic, extracted from execute_batch (FC-038).

    Pinned as its own unit because three call sites now share it: call sizing,
    batch selection, and the execution-time oversell guard. A regression here
    either starves the call side or sells naked.
    """

    def setup_method(self):
        self.engine = ExecutionEngine(Mock(), Mock(spec=Config))

    def test_no_position_means_nothing_available(self):
        assert self.engine._available_shares("AAPL", []) == (0, 0, 0)

    def test_owned_shares_are_available(self):
        assert self.engine._available_shares("AAPL", [_equity("AAPL", 100)]) == (100, 100, 0)
        assert self.engine._available_shares("AAPL", [_equity("AAPL", 200)]) == (200, 200, 0)

    def test_a_short_call_commits_the_shares_backing_it(self):
        positions = [_equity("AAPL", 100), _short_option("AAPL260724C00340000")]

        assert self.engine._available_shares("AAPL", positions) == (0, 100, 100)

    def test_partial_commitment_leaves_the_remainder_available(self):
        positions = [_equity("AAPL", 300), _short_option("AAPL260724C00340000", qty=-2)]

        assert self.engine._available_shares("AAPL", positions) == (100, 300, 200)

    def test_a_short_put_never_commits_shares(self):
        """FC-052 parity: `'C' in symbol` counted CVX puts as committed calls."""
        positions = [_equity("CVX", 100), _short_option("CVX260724P00140000")]

        assert self.engine._available_shares("CVX", positions) == (100, 100, 0)

    def test_a_long_call_does_not_commit_shares(self):
        """Only SHORT calls are covered by shares; a long call is an asset."""
        positions = [_equity("AAPL", 100),
                     {"symbol": "AAPL260724C00340000", "qty": "1",
                      "asset_class": "us_option", "side": "long"}]

        assert self.engine._available_shares("AAPL", positions) == (100, 100, 0)

    def test_another_underlyings_call_does_not_commit_these_shares(self):
        positions = [_equity("AAPL", 100), _equity("GOOGL", 100),
                     _short_option("GOOGL260724C00370000")]

        assert self.engine._available_shares("AAPL", positions) == (100, 100, 0)

    def test_malformed_position_cannot_inflate_availability(self):
        positions = [_equity("AAPL", 100),
                     {"symbol": "AAPL", "qty": "not-a-number",
                      "asset_class": "us_equity"}]

        available, _, _ = self.engine._available_shares("AAPL", positions)
        assert available == 100

    def test_missing_underlying_is_zero_not_a_crash(self):
        assert self.engine._available_shares(None, [_equity("AAPL", 100)]) == (0, 0, 0)

    def test_snapshot_fetch_failure_fails_closed(self):
        """No positions data must mean "sell nothing", never "sell naked"."""
        alpaca = Mock()
        alpaca.get_positions.side_effect = RuntimeError("Alpaca 503")
        engine = ExecutionEngine(alpaca, Mock(spec=Config))

        assert engine._available_shares("AAPL", engine._positions_snapshot()) == (0, 0, 0)


class TestSharesBasedCallSizing:
    """rank_opportunities sizes calls from shares, never from buying power.

    THE SIZING-STAGE BUG: every opportunity went through
    put_seller._calculate_position_size, whose cap is
    `buying_power // (strike * 100)`. On 2026-07-14, runs with BP $621 and
    $383 dropped *every* opportunity here -- including three AAPL calls backed
    by 100 idle shares that needed no cash at all.
    """

    def setup_method(self):
        self.alpaca = Mock()
        self.engine = ExecutionEngine(self.alpaca, Mock(spec=Config))
        self.engine.logger = Mock()
        self.put_seller = Mock(spec=PutSeller)
        self.put_seller._calculate_position_size.return_value = {"contracts": 1}

    def _rank(self, opps, positions, bp=0.0):
        self.alpaca.get_positions.return_value = positions
        return self.engine.rank_opportunities(opps, self.put_seller, bp)

    def test_call_is_sized_at_zero_buying_power(self):
        """THE FC-038 REGRESSION. Fails on the old code: BP $0 drops the call."""
        ranked = self._rank([_call("AAPL", 337.5)], [_equity("AAPL", 100)], bp=0.0)

        assert len(ranked) == 1
        assert ranked[0]["opportunity"]["contracts"] == 1
        assert ranked[0]["collateral"] == 0, "a covered call reserves no cash"
        self.put_seller._calculate_position_size.assert_not_called()

    def test_two_hundred_shares_size_two_contracts(self):
        ranked = self._rank([_call("AAPL", 337.5)], [_equity("AAPL", 200)])

        assert ranked[0]["opportunity"]["contracts"] == 2
        assert ranked[0]["premium"] == pytest.approx(1.0 * 100 * 2)

    def test_no_shares_drops_the_call_with_a_reason(self):
        ranked = self._rank([_call("AAPL", 337.5)], [])

        assert ranked == []
        drops = _drop_events(self.engine.logger)
        assert len(drops) == 1
        assert drops[0]["reason"] == "insufficient_available_shares"
        assert drops[0]["symbol"] == "AAPL"
        assert drops[0]["stage"] == "ranking"

    def test_fully_committed_shares_drop_the_call_with_a_reason(self):
        """The wasted-slot case: 100 owned, 100 already backing a short call."""
        ranked = self._rank(
            [_call("AAPL", 345)],
            [_equity("AAPL", 100), _short_option("AAPL260724C00340000")],
        )

        assert ranked == []
        drops = _drop_events(self.engine.logger)
        assert drops[0]["reason"] == "insufficient_available_shares"
        assert drops[0]["owned_shares"] == 100
        assert drops[0]["committed_to_calls"] == 100

    def test_odd_lot_below_one_contract_is_dropped(self):
        ranked = self._rank([_call("AAPL", 337.5)], [_equity("AAPL", 99)])

        assert ranked == []

    def test_puts_are_still_sized_by_the_put_seller(self):
        """Non-goal guard: put sizing must not change at all."""
        ranked = self._rank([_put("MSFT", 375.0, premium=2.56)], [], bp=GOLDEN_BP)

        self.put_seller._calculate_position_size.assert_called_once()
        assert self.put_seller._calculate_position_size.call_args.kwargs[
            "override_buying_power"] == GOLDEN_BP
        assert ranked[0]["collateral"] == 37500.0

    def test_put_sizing_failure_is_logged_not_silent(self):
        self.put_seller._calculate_position_size.return_value = None

        ranked = self._rank([_put("MSFT", 375.0)], [], bp=100.0)

        assert ranked == []
        assert _drop_events(self.engine.logger)[0]["reason"] == "sizing_failed"

    def test_call_type_is_taken_from_the_occ_symbol_not_the_dict_key(self):
        """Same routing rule execute_batch uses (FC-048): the contract wins."""
        mislabelled = _call("AAPL", 337.5)
        mislabelled["type"] = "put"          # producer lied

        ranked = self._rank([mislabelled], [_equity("AAPL", 100)], bp=0.0)

        assert len(ranked) == 1
        self.put_seller._calculate_position_size.assert_not_called()

    def test_positions_are_not_fetched_for_an_all_put_batch(self):
        """Puts need no share data; don't spend an API call every cycle."""
        self._rank([_put("MSFT", 375.0)], [], bp=GOLDEN_BP)

        self.alpaca.get_positions.assert_not_called()


class TestTwoPoolSelection:
    """select_batch runs two budgets: shares for calls, cash for puts.

    THE SELECTION-STAGE BUG: `collateral = strike * 100 * contracts` was
    charged against buying power for calls too. On 2026-07-17 a single GOOGL
    call charged $37,000 of phantom collateral and pushed all three AAPL calls
    out of a $67,142.50 budget.
    """

    def setup_method(self):
        self.alpaca = Mock()
        self.engine = ExecutionEngine(self.alpaca, Mock(spec=Config))
        self.engine.logger = Mock()

    def _select(self, items, bp, positions=None):
        self.alpaca.get_positions.return_value = positions or []
        return self.engine.select_batch(items, bp)

    @staticmethod
    def _item(opp, collateral=0.0, premium=100.0, roi=0.0):
        return {"opportunity": opp, "collateral": collateral, "premium": premium,
                "roi": roi, "type": opp.get("type", "put")}

    def test_call_is_selected_at_zero_buying_power(self):
        """THE FC-038 REGRESSION: a covered call needs no cash."""
        opp = _call("AAPL", 337.5, contracts=1)

        selected, remaining = self._select(
            [self._item(opp)], 0.0, [_equity("AAPL", 100)])

        assert [o["option_symbol"] for o in selected] == [opp["option_symbol"]]
        assert remaining == 0.0, "a call must not consume buying power"

    def test_a_call_does_not_crowd_out_a_put(self):
        """The 2026-07-17 reproduction, in miniature."""
        call = _call("GOOGL", 370.0, contracts=1)
        put = _put("IWM", 289.0, premium=0.68, contracts=1)

        selected, remaining = self._select(
            [self._item(call), self._item(put, collateral=28900.0, roi=0.0024)],
            30000.0, [_equity("GOOGL", 100)])

        assert [o["symbol"] for o in selected] == ["GOOGL", "IWM"], (
            "the call charged phantom collateral and starved the put again")
        assert remaining == pytest.approx(1100.0)

    def test_share_committed_underlying_is_dropped_at_selection(self):
        """The wasted-slot bug: this used to surface only at execution time."""
        opp = _call("GOOGL", 370.0, contracts=1)

        selected, _ = self._select(
            [self._item(opp)], 100000.0,
            [_equity("GOOGL", 100), _short_option("GOOGL260724C00365000")])

        assert selected == []
        drops = _drop_events(self.engine.logger)
        assert drops[0]["reason"] == "insufficient_available_shares"
        assert drops[0]["stage"] == "selection"

    def test_the_share_ledger_bounds_contract_count(self):
        """200 shares back two contracts, not three."""
        selected, _ = self._select(
            [self._item(_call("AAPL", 337.5, contracts=2))], 0.0,
            [_equity("AAPL", 200)])
        assert len(selected) == 1

        self.engine.logger = Mock()
        selected, _ = self._select(
            [self._item(_call("AAPL", 337.5, contracts=3))], 0.0,
            [_equity("AAPL", 200)])
        assert selected == []
        assert _drop_events(self.engine.logger)[0][
            "reason"] == "insufficient_available_shares"

    def test_puts_are_still_gated_by_buying_power(self):
        """Non-goal guard: the cash budget is unchanged for puts."""
        cheap = _put("IWM", 289.0, contracts=1)
        dear = _put("MSFT", 375.0, contracts=1)

        selected, remaining = self._select(
            [self._item(cheap, collateral=28900.0, roi=0.0024),
             self._item(dear, collateral=37500.0, roi=0.0068)],
            30000.0)

        assert [o["symbol"] for o in selected] == ["IWM"]
        assert remaining == pytest.approx(1100.0)
        assert [d["reason"] for d in _drop_events(self.engine.logger)] == [
            "insufficient_buying_power"]

    def test_dedup_is_global_across_both_pools(self):
        """One position per underlying, whichever pool it comes from."""
        selected, _ = self._select(
            [self._item(_call("AAPL", 337.5, contracts=1)),
             self._item(_put("AAPL", 300.0, contracts=1), collateral=30000.0, roi=0.01)],
            100000.0, [_equity("AAPL", 100)])

        assert len(selected) == 1
        assert selected[0]["type"] == "call"
        assert [d["reason"] for d in _drop_events(self.engine.logger)] == [
            "duplicate_underlying"]

    def test_calls_rank_by_attractiveness_score_not_roi(self):
        """Naive roi = premium/collateral is 0 for calls and sorts them last."""
        low = _call("AAPL", 337.5, score=70.0, contracts=1)
        high = _call("GOOGL", 370.0, score=90.0, contracts=1)

        selected, _ = self._select(
            [self._item(low), self._item(high)], 0.0,
            [_equity("AAPL", 100), _equity("GOOGL", 100)])

        assert [o["symbol"] for o in selected] == ["GOOGL", "AAPL"]

    def test_call_ranking_falls_back_to_premium_yield_without_a_score(self):
        thin = _call("AAPL", 340.0, premium=0.50, contracts=1)
        rich = _call("GOOGL", 340.0, premium=3.00, contracts=1)

        selected, _ = self._select(
            [self._item(thin), self._item(rich)], 0.0,
            [_equity("AAPL", 100), _equity("GOOGL", 100)])

        assert [o["symbol"] for o in selected] == ["GOOGL", "AAPL"]

    def test_calls_are_selected_before_puts(self):
        selected, _ = self._select(
            [self._item(_put("MSFT", 375.0, contracts=1), collateral=37500.0, roi=0.0068),
             self._item(_call("AAPL", 337.5, contracts=1))],
            100000.0, [_equity("AAPL", 100)])

        assert [o["symbol"] for o in selected] == ["AAPL", "MSFT"]

    def test_every_dropped_opportunity_carries_a_reason(self):
        items = [
            self._item(_call("AAPL", 337.5, contracts=1)),          # selected
            self._item(_call("AAPL", 340.0, contracts=1)),          # duplicate
            self._item(_call("NVDA", 220.0, contracts=1)),          # no shares
            self._item(_put("MSFT", 375.0, contracts=1), collateral=37500.0, roi=0.0068),
            self._item(_put("IWM", 289.0, contracts=1), collateral=28900.0, roi=0.0024),
        ]

        selected, _ = self._select(items, 40000.0, [_equity("AAPL", 100)])

        assert [o["symbol"] for o in selected] == ["AAPL", "MSFT"]
        assert sorted(d["reason"] for d in _drop_events(self.engine.logger)) == [
            "duplicate_underlying",
            "insufficient_available_shares",
            "insufficient_buying_power",
        ]

    def test_batch_summary_reports_drop_counts(self):
        self._select(
            [self._item(_call("NVDA", 220.0, contracts=1)),
             self._item(_put("MSFT", 375.0, contracts=1), collateral=37500.0)],
            100.0)

        summary = next(c.kwargs for c in self.engine.logger.info.call_args_list
                       if c.kwargs.get("event_type") == "batch_selection_completed")
        assert summary["dropped_count"] == 2
        assert summary["dropped_insufficient_available_shares"] == 1
        assert summary["dropped_insufficient_buying_power"] == 1
        assert summary["calls_selected"] == 0 and summary["puts_selected"] == 0


class TestGoldenReplay20260717:
    """Replay of the archived scan that produced the outage.

    Fixture: gs://options-wheel-opportunities/opportunities/2026-07-17/14-00.json
    -- 12 opportunities, six calls and six puts, copied verbatim. Its own
    `execution_results` record what the buggy pipeline did with $67,142.50 of
    buying power: GOOGL 370C (charged $37,000 of phantom collateral) plus
    IWM 289P, and nothing else. AAPL's calls were the two top-scored items in
    the batch (87.24 / 87.19) and were dropped.

    Portfolio value is set so the 35% per-position cap is never the binding
    constraint; buying power is the variable under test, at its production value.
    """

    def setup_method(self):
        with open(FIXTURES / "scan_2026-07-17_14-00.json") as f:
            self.blob = json.load(f)
        self.opportunities = copy.deepcopy(self.blob["opportunities"])

        self.alpaca = Mock()
        self.alpaca.get_account.return_value = {
            "portfolio_value": "120000.0",
            "buying_power": str(GOLDEN_BP),
            "options_buying_power": str(GOLDEN_BP),
        }
        self.config = Mock(spec=Config)
        self.config.max_position_size = 0.35        # config/settings.yaml
        self.engine = ExecutionEngine(self.alpaca, self.config)
        self.engine.logger = Mock()
        # A real PutSeller: the put path stays pinned to production sizing.
        self.put_seller = PutSeller(self.alpaca, Mock(), self.config)

    def _run(self, positions, bp=GOLDEN_BP):
        self.alpaca.get_positions.return_value = positions
        ranked = self.engine.rank_opportunities(self.opportunities, self.put_seller, bp)
        return self.engine.select_batch(ranked, bp)

    def test_the_fixture_is_the_incident(self):
        """Guard the fixture: a silent edit would hollow out this whole class."""
        assert self.blob["opportunity_count"] == 12
        assert len(self.opportunities) == 12

        executed = [r["opportunity"]["option_symbol"]
                    for r in self.blob["execution_results"]]
        assert executed == ["GOOGL260724C00370000", "IWM260721P00289000"], (
            "the fixture no longer records the buggy outcome it exists to document")

        top = max(self.opportunities, key=lambda o: o["attractiveness_score"])
        assert top["option_symbol"] == "AAPL260720C00337500"

    def test_aapls_top_call_is_selected(self):
        """The point of FC-038: AAPL was starved for four trading days."""
        selected, _ = self._run([_equity("AAPL", 100), _equity("GOOGL", 100)])

        assert "AAPL260720C00337500" in [o["option_symbol"] for o in selected]

    def test_the_puts_that_actually_fit_are_still_selected(self):
        """Calls must not displace the cash pool -- they consume none of it."""
        selected, remaining = self._run([_equity("AAPL", 100), _equity("GOOGL", 100)])

        symbols = [o["option_symbol"] for o in selected]
        assert "MSFT260724P00375000" in symbols
        assert "IWM260721P00289000" in symbols
        # 67,142.50 - 37,500 (MSFT 375) - 28,900 (IWM 289)
        assert remaining == pytest.approx(742.50)

    def test_one_position_per_underlying_across_twelve_opportunities(self):
        selected, _ = self._run([_equity("AAPL", 100), _equity("GOOGL", 100)])

        underlyings = [o["symbol"] for o in selected]
        assert sorted(underlyings) == ["AAPL", "GOOGL", "IWM", "MSFT"]

        googl = [o for o in selected if o["symbol"] == "GOOGL"]
        assert len(googl) == 1
        assert googl[0]["option_symbol"] == "GOOGL260722C00370000", (
            "GOOGL's one slot should go to its best-scored call")

    def test_nothing_is_dropped_without_a_reason(self):
        selected, _ = self._run([_equity("AAPL", 100), _equity("GOOGL", 100)])

        drops = _drop_events(self.engine.logger)
        selected_symbols = {o["option_symbol"] for o in selected}
        dropped_symbols = {d["option_symbol"] for d in drops}
        all_symbols = {o["option_symbol"] for o in self.opportunities}

        assert selected_symbols | dropped_symbols == all_symbols
        assert not selected_symbols & dropped_symbols
        assert all(d["reason"] in DROP_REASONS for d in drops)
        assert len(_select_events(self.engine.logger)) == len(selected)

    def test_committed_googl_shares_no_longer_burn_a_slot(self):
        """7/15-7/16: GOOGL was re-selected every cycle and never executable."""
        selected, _ = self._run([
            _equity("AAPL", 100),
            _equity("GOOGL", 100),
            _short_option("GOOGL260724C00370000"),   # the fill from this cycle
        ])

        assert "GOOGL" not in [o["symbol"] for o in selected]
        googl_drops = [d for d in _drop_events(self.engine.logger)
                       if d["symbol"] == "GOOGL"]
        assert len(googl_drops) == 3
        assert {d["reason"] for d in googl_drops} == {"insufficient_available_shares"}
        assert "AAPL260720C00337500" in [o["option_symbol"] for o in selected]

    def test_calls_survive_the_cash_drought_that_killed_them_on_0714(self):
        """BP $621 dropped every opportunity at the sizing stage on 2026-07-14."""
        selected, remaining = self._run(
            [_equity("AAPL", 100), _equity("GOOGL", 100)], bp=621.0)

        assert [o["option_symbol"] for o in selected] == [
            "AAPL260720C00337500", "GOOGL260722C00370000"]
        assert remaining == 621.0

        put_drops = [d for d in _drop_events(self.engine.logger)
                     if d["opportunity_type"] == "put"]
        assert len(put_drops) == 6
        assert {d["reason"] for d in put_drops} == {"sizing_failed"}


class TestSingleSnapshotPerCycle:
    """Sizing and selection must reason about the SAME positions (review F2).

    Two fetches means a fill landing between them can produce a selection the
    sizing stage never sanctioned -- and it doubles the broker calls on the
    hot path for no benefit.
    """

    def setup_method(self):
        self.alpaca = Mock()
        self.engine = ExecutionEngine(self.alpaca, Mock(spec=Config))
        self.engine.logger = Mock()
        self.put_seller = Mock(spec=PutSeller)
        self.put_seller._calculate_position_size.return_value = {"contracts": 1}

    def test_a_threaded_snapshot_is_never_re_fetched(self):
        snapshot = [_equity("AAPL", 100)]

        ranked = self.engine.rank_opportunities(
            [_call("AAPL", 337.5), _put("MSFT", 375.0)], self.put_seller, 67142.50,
            positions=snapshot)
        selected, _ = self.engine.select_batch(ranked, 67142.50, positions=snapshot)

        self.alpaca.get_positions.assert_not_called()
        assert sorted(o["symbol"] for o in selected) == ["AAPL", "MSFT"]

    def test_the_run_endpoint_threads_one_snapshot_into_both_stages(self):
        """Pin the wiring, not just the capability -- /run is what ships."""
        source = Path("deploy/cloud_run_server.py").read_text()

        assert "positions_snapshot = exec_engine._positions_snapshot()" in source
        rank_call = source.split("exec_engine.rank_opportunities(")[1].split(")")[0]
        select_call = source.split("exec_engine.select_batch(")[1].split(")")[0]
        assert "positions=positions_snapshot" in rank_call
        assert "positions=positions_snapshot" in select_call

    def test_an_empty_snapshot_is_honoured_not_treated_as_absent(self):
        """`[]` means "we looked, you own nothing" -- do not go re-fetch."""
        self.engine.rank_opportunities(
            [_call("AAPL", 337.5)], self.put_seller, 0.0, positions=[])

        self.alpaca.get_positions.assert_not_called()


class TestPositionsUnavailableIsNotZeroShares:
    """A failed positions fetch must not read as "the account owns nothing".

    Both are empty, both block the trade -- but one is a normal Tuesday and
    the other is an outage silently halting every covered call in the fleet.
    Collapsing them into `insufficient_available_shares` makes the outage
    unfindable, which is the exact failure mode FC-038 exists to end.
    """

    def setup_method(self):
        self.alpaca = Mock()
        self.alpaca.get_positions.side_effect = RuntimeError("Alpaca 503")
        self.engine = ExecutionEngine(self.alpaca, Mock(spec=Config))
        self.engine.logger = Mock()
        self.put_seller = Mock(spec=PutSeller)

    def test_snapshot_failure_is_its_own_reason_at_ranking(self):
        ranked = self.engine.rank_opportunities(
            [_call("AAPL", 337.5)], self.put_seller, 0.0)

        assert ranked == []
        drops = _drop_events(self.engine.logger)
        assert [d["reason"] for d in drops] == ["positions_unavailable"]

    def test_snapshot_failure_is_its_own_reason_at_selection(self):
        item = {"opportunity": _call("AAPL", 337.5, contracts=1), "collateral": 0.0,
                "premium": 100.0, "roi": 0.0, "type": "call"}

        selected, _ = self.engine.select_batch([item], 0.0)

        assert selected == []
        drops = _drop_events(self.engine.logger)
        assert [d["reason"] for d in drops] == ["positions_unavailable"]

    def test_a_genuinely_empty_account_still_says_insufficient_shares(self):
        """Don't over-correct: a real empty snapshot keeps its own reason."""
        self.alpaca.get_positions.side_effect = None
        self.alpaca.get_positions.return_value = []

        self.engine.rank_opportunities([_call("AAPL", 337.5)], self.put_seller, 0.0)

        assert [d["reason"] for d in _drop_events(self.engine.logger)] == [
            "insufficient_available_shares"]

    def test_the_reason_survives_the_batch_summary(self):
        item = {"opportunity": _call("AAPL", 337.5, contracts=1), "collateral": 0.0,
                "premium": 100.0, "roi": 0.0, "type": "call"}

        self.engine.select_batch([item], 0.0)

        summary = next(c.kwargs for c in self.engine.logger.info.call_args_list
                       if c.kwargs.get("event_type") == "batch_selection_completed")
        assert summary["dropped_positions_unavailable"] == 1
        assert summary["dropped_insufficient_available_shares"] == 0

    def test_the_sentinel_still_fails_closed_everywhere(self):
        """It must behave as an empty list; nothing may be sold on no data."""
        snapshot = self.engine._positions_snapshot()

        assert isinstance(snapshot, PositionsUnavailable)
        assert snapshot == [] and not snapshot
        assert self.engine._available_shares("AAPL", snapshot) == (0, 0, 0)

    def test_the_execution_guard_flags_the_outage_too(self):
        call_seller = Mock(spec=CallSeller)
        self.engine.execute_batch(
            [{"symbol": "AAPL", "option_symbol": CALL_SYM, "strategy": "sell_call",
              "contracts": 1, "premium": 1.0, "strike_price": 190}],
            Mock(spec=PutSeller), call_seller=call_seller)

        call_seller.execute_call_sale.assert_not_called()
        blocked = next(c.kwargs for c in self.engine.logger.warning.call_args_list
                       if c.kwargs.get("event_type") == "naked_call_blocked")
        assert blocked["positions_unavailable"] is True


class TestEventsCarryUnambiguousPremiumUnits:
    """`premium` meant per-share on one event and total dollars on the other.

    Any query spanning both silently mixed scales by a factor of 100 x
    contracts, so both events now name their units.
    """

    def setup_method(self):
        self.alpaca = Mock()
        self.engine = ExecutionEngine(self.alpaca, Mock(spec=Config))
        self.engine.logger = Mock()
        self.put_seller = Mock(spec=PutSeller)
        self.put_seller._calculate_position_size.return_value = {"contracts": 1}

    def test_selection_event_names_both_units(self):
        self.alpaca.get_positions.return_value = [_equity("AAPL", 200)]

        ranked = self.engine.rank_opportunities(
            [_call("AAPL", 337.5, premium=0.755)], self.put_seller, 0.0)
        self.engine.select_batch(ranked, 0.0)

        event = _select_events(self.engine.logger)[0]
        assert event["contracts"] == 2
        assert event["premium_per_share"] == pytest.approx(0.755)
        assert event["total_premium"] == pytest.approx(151.0)   # 0.755 x 100 x 2
        assert "premium" not in event, "the ambiguous key must be gone"

    def test_drop_event_names_both_units(self):
        self.alpaca.get_positions.return_value = [_equity("AAPL", 100)]

        ranked = self.engine.rank_opportunities(
            [_call("AAPL", 337.5, premium=0.755, score=87.2),
             _call("AAPL", 340.0, premium=1.145, score=87.1)],
            self.put_seller, 0.0)
        self.engine.select_batch(ranked, 0.0)

        drop = _drop_events(self.engine.logger)[0]
        assert drop["reason"] == "duplicate_underlying"
        assert drop["contracts"] == 1
        assert drop["premium_per_share"] == pytest.approx(1.145)
        assert drop["total_premium"] == pytest.approx(114.5)
        assert "premium" not in drop

    def test_a_drop_before_sizing_reports_no_total(self):
        """Honest nulls beat a total invented from an assumed contract count."""
        self.alpaca.get_positions.return_value = []

        self.engine.rank_opportunities(
            [_call("AAPL", 337.5, premium=0.755)], self.put_seller, 0.0)

        drop = _drop_events(self.engine.logger)[0]
        assert drop["premium_per_share"] == pytest.approx(0.755)
        assert drop["total_premium"] is None
        assert drop["contracts"] is None


class TestDropReasonsAreExposedForTheDecisionRecord:
    """FC-065 Phase 4: `/run` needs to say WHY a symbol was not traded.

    The reasons already existed on the `selection_dropped` log event, which
    lives only in Cloud Logging's 30-day window (the BigQuery sink died —
    FC-046). They are now also published on the engine so the durable
    decision record can carry them, from the single `_log_drop` chokepoint
    that both ranking and selection already share.
    """

    def setup_method(self):
        self.alpaca = Mock()
        self.engine = ExecutionEngine(self.alpaca, Mock(spec=Config))
        self.engine.logger = Mock()
        self.put_seller = Mock(spec=PutSeller)
        self.put_seller._calculate_position_size.return_value = {"contracts": 1}

    def test_starts_empty(self):
        assert self.engine.last_call_drop_reasons == {}

    def test_a_ranking_drop_is_published(self):
        self.alpaca.get_positions.return_value = []

        self.engine.rank_opportunities(
            [_call("AAPL", 337.5)], self.put_seller, 0.0)

        assert self.engine.last_call_drop_reasons == {
            "AAPL": "insufficient_available_shares"}

    def test_a_selection_drop_is_published(self):
        self.alpaca.get_positions.return_value = [_equity("AAPL", 200)]

        ranked = self.engine.rank_opportunities(
            [_call("AAPL", 337.5, score=87.2), _call("AAPL", 340.0, score=87.1)],
            self.put_seller, 0.0)
        self.engine.select_batch(ranked, 0.0)

        assert self.engine.last_call_drop_reasons == {"AAPL": "duplicate_underlying"}

    def test_a_positions_outage_is_not_reported_as_no_shares(self):
        # The two demand opposite responses; the decision record must be able
        # to tell them apart just as the log event can.
        self.alpaca.get_positions.side_effect = Exception("broker down")

        self.engine.rank_opportunities(
            [_call("AAPL", 337.5)], self.put_seller, 0.0)

        assert self.engine.last_call_drop_reasons == {"AAPL": "positions_unavailable"}

    def test_a_selected_symbol_has_no_drop_reason(self):
        self.alpaca.get_positions.return_value = [_equity("AAPL", 100)]

        ranked = self.engine.rank_opportunities(
            [_call("AAPL", 337.5)], self.put_seller, 0.0)
        selected, _ = self.engine.select_batch(ranked, 0.0)

        assert len(selected) == 1
        assert self.engine.last_call_drop_reasons == {}

    def test_a_dropped_put_never_becomes_a_calls_drop_reason(self):
        """Only one position per underlying is allowed across BOTH pools.

        So a selected AAPL call drops the AAPL put as `duplicate_underlying`.
        A symbol-keyed map holding both would hand the covered-call decision
        record the PUT's reason for a symbol whose call was never dropped at
        all — a wrong answer in the table the operator alert reads.
        """
        self.alpaca.get_positions.return_value = [_equity("AAPL", 100)]

        ranked = self.engine.rank_opportunities(
            [_call("AAPL", 337.5, score=90.0), _put("AAPL", 300.0)],
            self.put_seller, 1_000_000.0)
        selected, _ = self.engine.select_batch(ranked, 1_000_000.0)

        # The call was selected; the put was dropped as a duplicate.
        assert [o["option_symbol"] for o in selected] == ["AAPL260724C00337500"]
        dropped = _drop_events(self.engine.logger)
        assert [d["reason"] for d in dropped] == ["duplicate_underlying"]
        assert dropped[0]["opportunity_type"] == "put"
        # ...and none of that reached the covered-call channel.
        assert self.engine.last_call_drop_reasons == {}

    def test_a_new_cycle_does_not_inherit_the_previous_one(self):
        """Last hour's reason reported as this hour's decision is a lie."""
        self.alpaca.get_positions.return_value = []
        self.engine.rank_opportunities(
            [_call("AAPL", 337.5)], self.put_seller, 0.0)
        assert self.engine.last_call_drop_reasons

        self.alpaca.get_positions.return_value = [_equity("MSFT", 100)]
        self.engine.rank_opportunities(
            [_call("MSFT", 400.0)], self.put_seller, 0.0)

        assert "AAPL" not in self.engine.last_call_drop_reasons


# ======================================================================
# FC-041 — class-share tickers, and the parser-independent invariant
#
# Alpaca's equity symbol for Berkshire's B shares is `BRK.B`; the contracts
# written on those shares carry the OCC root `BRKB` (verified against the paper
# API 2026-08-28: `get_asset('BRK.B').symbol == 'BRK.B'`, and
# `get_option_contracts(underlying_symbols=['BRK.B'])` -> `BRKB260828C00270000`,
# `root_symbol='BRKB'`). `_available_shares` joined those two spellings as raw
# strings, so `committed` came back 0 for every dotted ticker, `available` came
# back equal to `owned`, and the engine would write a second call over shares
# already sold. Latent for the wheel (no dotted ticker in `stocks.symbols`);
# NOT latent for the covered-call account, which has no configured universe.
# ======================================================================

BRK_EQUITY = "BRK.B"          # what Alpaca renders in /positions
BRK_CALL = "BRKB260918C00450000"
BRK_CALL_2 = "BRKB260918C00460000"


def _brk_call(option_symbol=BRK_CALL_2, contracts=1):
    """A covered-call opportunity as the scanner emits it for a class share.

    `symbol` is the EQUITY spelling and `option_symbol` is the OCC spelling —
    that mismatch is the entire defect, so it must be in the fixture.
    """
    return {"symbol": BRK_EQUITY, "option_symbol": option_symbol,
            "type": "call", "strike_price": 460.0, "premium": 1.5,
            "contracts": contracts, "shares_covered": contracts * 100,
            "stock_cost_basis": 400.0}


class TestClassShareTickersJoinOnTheOccRoot:
    """FC-041 test 2 — the regression itself, at the ledger.

    Pre-fix this returns (200, 200, 0): the short BRKB call is invisible to a
    raw-string join against `BRK.B`, so 200 shares read as fully available and
    a second contract would be written naked.
    """

    def setup_method(self):
        self.engine = ExecutionEngine(Mock(), Mock(spec=Config))

    def test_a_short_call_on_the_occ_root_commits_the_dotted_tickers_shares(self):
        """THE FC-041 REGRESSION. Fails pre-fix with (200, 200, 0)."""
        positions = [_equity(BRK_EQUITY, 200), _short_option(BRK_CALL)]

        assert self.engine._available_shares(BRK_EQUITY, positions) == (100, 200, 100)

    def test_it_holds_when_the_caller_passes_the_occ_root_instead(self):
        """Both sides normalize, so either spelling of the argument works."""
        positions = [_equity(BRK_EQUITY, 200), _short_option(BRK_CALL)]

        assert self.engine._available_shares("BRKB", positions) == (100, 200, 100)

    def test_fully_committed_class_shares_report_zero_available(self):
        positions = [_equity(BRK_EQUITY, 100), _short_option(BRK_CALL)]

        assert self.engine._available_shares(BRK_EQUITY, positions) == (0, 100, 100)

    def test_a_short_PUT_on_the_root_still_commits_nothing(self):
        """The normalization must not resurrect the `'C' in symbol` family."""
        positions = [_equity(BRK_EQUITY, 100),
                     _short_option("BRKB260918P00400000")]

        assert self.engine._available_shares(BRK_EQUITY, positions) == (100, 100, 0)

    def test_plain_tickers_are_byte_identical(self):
        """The behavior contract: nothing moves for the live universe."""
        positions = [_equity("AAPL", 300), _short_option("AAPL260724C00340000", qty=-2)]

        assert self.engine._available_shares("AAPL", positions) == (100, 300, 200)

    def test_a_different_class_of_the_same_issuer_is_not_the_same_underlying(self):
        """BRK.A shares must not be counted as backing a BRKB call."""
        positions = [_equity("BRK.A", 200), _short_option(BRK_CALL)]

        assert self.engine._available_shares(BRK_EQUITY, positions) == (-100, 0, 100)


class TestClassShareCallIsDroppedAtSelection:
    """FC-041 test 3 — the ledger fix reaching `select_batch`.

    100 shares, all of them already committed to a short BRKB call: a second
    BRKB call must be dropped exactly as it would be for AAPL. Pre-fix the
    share ledger reads 100 available and the call is SELECTED.
    """

    def setup_method(self):
        self.alpaca = Mock()
        self.engine = ExecutionEngine(self.alpaca, Mock(spec=Config))
        self.engine.logger = Mock()

    def test_a_committed_class_share_underlying_is_dropped(self):
        item = {"opportunity": _brk_call(), "collateral": 0.0,
                "premium": 150.0, "roi": 0.0, "type": "call"}
        self.alpaca.get_positions.return_value = [
            _equity(BRK_EQUITY, 100), _short_option(BRK_CALL)]

        selected, _ = self.engine.select_batch([item], 100000.0)

        assert selected == []
        drops = _drop_events(self.engine.logger)
        assert [(d["reason"], d["stage"]) for d in drops] == [
            ("insufficient_available_shares", "selection")]

    def test_an_uncommitted_class_share_call_is_still_selected(self):
        """The fix must not over-block: idle class shares still back a call."""
        item = {"opportunity": _brk_call(), "collateral": 0.0,
                "premium": 150.0, "roi": 0.0, "type": "call"}
        self.alpaca.get_positions.return_value = [_equity(BRK_EQUITY, 100)]

        selected, remaining = self.engine.select_batch([item], 0.0)

        assert [o["option_symbol"] for o in selected] == [BRK_CALL_2]
        assert remaining == 0.0


class TestNakedCallInvariant:
    """FC-041 tests 4 & 5 — the belt, independent of `_available_shares`.

    `_available_shares` is the braces. The invariant recounts the SAME snapshot
    with `_invariant_shares` (strict_option_type + occ_root only, no
    `parse_option_symbol`) and refuses the order when the two disagree. It can
    only be tested by making them disagree, which is what the monkeypatch does:
    it stands in for a future bug in the primary ledger, not for a state the
    current code can reach on its own.
    """

    def setup_method(self):
        from src.strategy.execution_engine import clear_failed_symbols
        clear_failed_symbols()
        self.alpaca = Mock()
        self.engine = ExecutionEngine(self.alpaca, Mock(spec=Config))
        self.engine.logger = Mock()
        self.put_seller = Mock(spec=PutSeller)
        self.call_seller = Mock(spec=CallSeller)
        self.put_seller.execute_put_sale.return_value = {"success": True, "order_id": "p1"}
        self.call_seller.execute_call_sale.return_value = {"success": True, "order_id": "c1"}

    def _errors(self, event_type):
        return [c.kwargs for c in self.engine.logger.error.call_args_list
                if c.kwargs.get("event_type") == event_type]

    def test_it_blocks_the_order_when_the_share_ledger_is_wrong(self):
        """FC-041 test 4. MUTATION CHECK: delete the invariant and this fails.

        The lie: `_available_shares` claims 100 free shares while the snapshot
        shows all 100 owned shares already committed to a short call. Gate 19
        is satisfied by the lie; gate 20 recounts and refuses.
        """
        self.alpaca.get_positions.return_value = [
            _equity("AAPL", 100), _short_option("AAPL260724C00340000")]
        self.engine._available_shares = Mock(return_value=(100, 100, 0))

        opp = {"symbol": "AAPL", "option_symbol": "AAPL260724C00350000",
               "type": "call", "contracts": 1, "premium": 1.0,
               "strike_price": 350.0, "shares_covered": 100,
               "stock_cost_basis": 200.0}
        results, count = self.engine.execute_batch(
            [opp], self.put_seller, call_seller=self.call_seller)

        self.call_seller.execute_call_sale.assert_not_called()
        assert count == 0
        assert results[0]["success"] is False
        assert results[0]["result"]["error_type"] == "naked_call_invariant"

        errors = self._errors("naked_call_invariant_blocked")
        assert len(errors) == 1
        assert (errors[0]["owned"], errors[0]["committed"],
                errors[0]["requested"]) == (100, 100, 100)
        assert errors[0]["symbol"] == "AAPL"

        drops = _drop_events(self.engine.logger)
        assert [(d["reason"], d["stage"]) for d in drops] == [
            ("naked_call_invariant", "execution")]

    def test_a_blocked_call_does_not_stop_the_rest_of_the_batch(self):
        """FC-041 test 4, second half: the batch continues."""
        self.alpaca.get_positions.return_value = [
            _equity("AAPL", 100), _short_option("AAPL260724C00340000")]
        self.engine._available_shares = Mock(return_value=(100, 100, 0))

        bad_call = {"symbol": "AAPL", "option_symbol": "AAPL260724C00350000",
                    "type": "call", "contracts": 1, "premium": 1.0,
                    "strike_price": 350.0, "shares_covered": 100,
                    "stock_cost_basis": 200.0}
        good_put = {"symbol": "MSFT", "option_symbol": "MSFT260724P00380000",
                    "type": "put", "contracts": 1, "premium": 2.5,
                    "strike_price": 380.0}

        results, count = self.engine.execute_batch(
            [bad_call, good_put], self.put_seller, call_seller=self.call_seller)

        assert len(results) == 2 and count == 1
        assert results[0]["success"] is False and results[1]["success"] is True
        self.put_seller.execute_put_sale.assert_called_once()

    def test_a_blocked_call_is_not_marked_non_retryable(self):
        """Share ownership is transient — the same contract can be legitimate
        next cycle, exactly as for `naked_call_blocked`."""
        from src.strategy.execution_engine import get_failed_symbols

        self.alpaca.get_positions.return_value = [
            _equity("AAPL", 100), _short_option("AAPL260724C00340000")]
        self.engine._available_shares = Mock(return_value=(100, 100, 0))

        self.engine.execute_batch(
            [{"symbol": "AAPL", "option_symbol": "AAPL260724C00350000",
              "type": "call", "contracts": 1, "premium": 1.0,
              "strike_price": 350.0, "shares_covered": 100,
              "stock_cost_basis": 200.0}],
            self.put_seller, call_seller=self.call_seller)

        assert "AAPL260724C00350000" not in get_failed_symbols()

    def test_it_does_not_fire_on_a_legitimate_write(self):
        """FC-041 test 5: owned 200, committed 100, selling 1 contract."""
        self.alpaca.get_positions.return_value = [
            _equity("AAPL", 200), _short_option("AAPL260724C00340000")]

        results, count = self.engine.execute_batch(
            [{"symbol": "AAPL", "option_symbol": "AAPL260724C00350000",
              "type": "call", "contracts": 1, "premium": 1.0,
              "strike_price": 350.0, "shares_covered": 100,
              "stock_cost_basis": 200.0}],
            self.put_seller, call_seller=self.call_seller)

        self.call_seller.execute_call_sale.assert_called_once()
        assert count == 1 and results[0]["success"] is True
        assert self._errors("naked_call_invariant_blocked") == []
        assert _drop_events(self.engine.logger) == []

    def test_it_does_not_fire_on_a_legitimate_class_share_write(self):
        """The two halves of FC-041 together: a BRK.B call over uncommitted
        class shares must go out, not be blocked by the new belt."""
        self.alpaca.get_positions.return_value = [_equity(BRK_EQUITY, 200),
                                                  _short_option(BRK_CALL)]

        results, count = self.engine.execute_batch(
            [_brk_call()], self.put_seller, call_seller=self.call_seller)

        self.call_seller.execute_call_sale.assert_called_once()
        assert count == 1 and results[0]["success"] is True
        assert self._errors("naked_call_invariant_blocked") == []

    def test_the_class_share_overwrite_is_stopped_by_gate_19_not_gate_20(self):
        """With the ledger fixed, a second BRKB call over 100 committed shares
        never reaches the invariant — it is refused as `naked_call_blocked`.
        Gate 20 firing here would mean gate 19 had regressed."""
        self.alpaca.get_positions.return_value = [_equity(BRK_EQUITY, 100),
                                                  _short_option(BRK_CALL)]

        results, count = self.engine.execute_batch(
            [_brk_call()], self.put_seller, call_seller=self.call_seller)

        self.call_seller.execute_call_sale.assert_not_called()
        assert count == 0 and results[0]["success"] is False
        assert "Call blocked:" in results[0]["result"]["message"]
        assert self._errors("naked_call_invariant_blocked") == []

    def test_the_recount_ignores_a_long_call_and_another_underlying(self):
        """`_invariant_shares` is a second implementation, so its own
        arithmetic needs pinning — a bug here blocks legitimate calls."""
        assert self.engine._invariant_shares("AAPL", [
            _equity("AAPL", 200),
            {"symbol": "AAPL260724C00340000", "qty": "1",
             "asset_class": "us_option", "side": "long"},
            _equity("GOOGL", 500),
            _short_option("GOOGL260724C00370000"),
            _short_option("AAPL260724P00300000"),
        ]) == (200, 0, [])

    def test_the_recount_joins_class_shares_on_the_occ_root_too(self):
        """Both sides of the belt normalize, or the belt would fire on every
        legitimate class-share call the moment the ledger was fixed."""
        assert self.engine._invariant_shares(BRK_EQUITY, [
            _equity(BRK_EQUITY, 200), _short_option(BRK_CALL)]) == (200, 100, [])

    def test_the_recount_fails_closed_on_no_underlying(self):
        assert self.engine._invariant_shares(None, [_equity("AAPL", 100)]) == (0, 0, [])
        assert self.engine._invariant_shares(
            "AAPL", PositionsUnavailable()) == (0, 0, [])


class TestNakedCallInvariantFailsClosedOnUnclassifiableShorts:
    """S1/S2 review finding F1 — the corporate-action hole.

    `strict_option_type` returns None for an ADJUSTED contract
    (`AAPL1260821C00250000` — the root a split or special dividend leaves
    behind). Both share counters therefore scored such a short call as
    committing **0 shares**, so gate 20 sailed past a real obligation against
    real shares and the docs claimed a fail-closed posture the code did not
    have. An unclassifiable short option whose leading-alpha prefix lands on
    the target root now blocks the write instead.
    """

    def setup_method(self):
        from src.strategy.execution_engine import clear_failed_symbols
        clear_failed_symbols()
        self.alpaca = Mock()
        self.engine = ExecutionEngine(self.alpaca, Mock(spec=Config))
        self.engine.logger = Mock()
        self.put_seller = Mock(spec=PutSeller)
        self.call_seller = Mock(spec=CallSeller)
        self.put_seller.execute_put_sale.return_value = {"success": True, "order_id": "p1"}
        self.call_seller.execute_call_sale.return_value = {"success": True, "order_id": "c1"}

    def _errors(self, event_type):
        return [c.kwargs for c in self.engine.logger.error.call_args_list
                if c.kwargs.get("event_type") == event_type]

    def _aapl_call(self, contracts=1):
        return {"symbol": "AAPL", "option_symbol": "AAPL260724C00350000",
                "type": "call", "contracts": contracts, "premium": 1.0,
                "strike_price": 350.0, "shares_covered": contracts * 100,
                "stock_cost_basis": 200.0}

    def test_an_adjusted_short_call_blocks_the_write(self):
        """F1 test (a). MUTATION CHECK: drop the unclassifiable limb and the
        adjusted call scores 0 committed, 100 owned covers the 100 requested,
        and this naked write goes out.
        """
        self.alpaca.get_positions.return_value = [
            _equity("AAPL", 100), _short_option("AAPL1260821C00250000")]

        results, count = self.engine.execute_batch(
            [self._aapl_call()], self.put_seller, call_seller=self.call_seller)

        self.call_seller.execute_call_sale.assert_not_called()
        assert count == 0 and results[0]["success"] is False
        assert results[0]["result"]["error_type"] == "naked_call_invariant"

        errors = self._errors("naked_call_invariant_blocked")
        assert len(errors) == 1
        assert errors[0]["unclassifiable_short_options"] == [
            "AAPL1260821C00250000"]

        drops = _drop_events(self.engine.logger)
        assert [(d["reason"], d["stage"]) for d in drops] == [
            ("naked_call_invariant", "execution")]
        assert drops[0]["unclassifiable_short_options"] == [
            "AAPL1260821C00250000"]

    def test_an_off_shape_short_call_blocks_even_with_shares_to_spare(self):
        """F1 test (b), and the parser-independence pin.

        `AAPL260724C00340000X` is rejected by `strict_option_type` but
        `parse_option_symbol` resolves it to AAPL/call via its last-resort
        heuristic. 200 shares are owned and only 100 are requested, so a
        `_invariant_shares` rewritten onto `parse_option_symbol` would score
        100 committed, find 100 + 100 <= 200, and let the write through.
        MUTATION CHECK: that rewrite fails this test.
        """
        self.alpaca.get_positions.return_value = [
            _equity("AAPL", 200), _short_option("AAPL260724C00340000X")]

        results, count = self.engine.execute_batch(
            [self._aapl_call()], self.put_seller, call_seller=self.call_seller)

        self.call_seller.execute_call_sale.assert_not_called()
        assert count == 0 and results[0]["success"] is False
        assert self._errors("naked_call_invariant_blocked")[0][
            "unclassifiable_short_options"] == ["AAPL260724C00340000X"]

    def test_gate_19_does_not_catch_either_of_them_first(self):
        """The finding only matters because `_available_shares` also scores an
        adjusted contract at 0 committed. Pinned so this class cannot silently
        start testing gate 19 instead of gate 20."""
        assert self.engine._available_shares("AAPL", [
            _equity("AAPL", 100), _short_option("AAPL1260821C00250000"),
        ]) == (100, 100, 0)
        assert self.engine._available_shares("AAPL", [
            _equity("AAPL", 200), _short_option("AAPL260724C00340000X"),
        ]) == (200, 200, 0)

    def test_unrelated_garbage_does_not_block_the_underlying(self):
        """Rewritten from `test_the_recount_refuses_to_classify_a_non_occ_symbol`,
        which pinned the fail-OPEN behavior this finding removed.

        The limb is root-prefix scoped on purpose: a position we cannot parse
        AND cannot tie to this underlying must not block every call in the
        account. `NOT_AN_OCC` -> prefix `NOT`; `1AAPL...` -> prefix empty.
        """
        assert self.engine._invariant_shares("AAPL", [
            _equity("AAPL", 100), _short_option("NOT_AN_OCC"),
            _short_option("1AAPL260724C00340000"),
        ]) == (100, 0, [])

    def test_it_is_type_blind_on_the_unclassifiable_limb(self):
        """An adjusted short PUT commits no shares, but we cannot prove it is
        a put without the parser we deliberately refuse to use here. Documented
        over-block: the cost is one skipped covered call."""
        assert self.engine._invariant_shares("AAPL", [
            _equity("AAPL", 200), _short_option("AAPL1260821P00250000"),
        ]) == (200, 0, ["AAPL1260821P00250000"])

    def test_a_long_unclassifiable_option_commits_nothing_and_blocks_nothing(self):
        """Only SHORT options are obligations."""
        assert self.engine._invariant_shares("AAPL", [
            _equity("AAPL", 200),
            {"symbol": "AAPL1260821C00250000", "qty": "1",
             "asset_class": "us_option", "side": "long"},
        ]) == (200, 0, [])

    def test_it_matches_a_class_share_root_through_the_prefix_rule(self):
        """The two findings meet: an adjusted BRKB contract must block a BRK.B
        call, which needs the prefix normalized too."""
        assert self.engine._invariant_shares(BRK_EQUITY, [
            _equity(BRK_EQUITY, 200), _short_option("BRKB1260918C00450000"),
        ]) == (200, 0, ["BRKB1260918C00450000"])

    def test_a_non_string_symbol_does_not_raise(self):
        """F6. A malformed field must not take the whole batch down with a
        TypeError; the posture matches `_available_shares` exactly, and the
        position still surfaces as the monitor's `risk_unclassifiable_option`.
        """
        positions = [_equity("AAPL", 200),
                     {"symbol": 12345, "qty": "-1",
                      "asset_class": "us_option", "side": "short"}]

        owned, committed, unclassifiable = self.engine._invariant_shares(
            "AAPL", positions)

        assert (owned, committed) == (200, 0)
        assert unclassifiable == []
        # Same answer as the primary ledger, which is the contract between them.
        assert self.engine._available_shares("AAPL", positions)[1:] == (
            owned, committed)

    def test_a_multi_contract_write_is_blocked_when_it_would_oversell(self):
        """F7. The arithmetic limb has to scale with `contracts`; pinned at 1
        contract only, `required_shares` could be hardcoded and nothing would
        notice."""
        self.alpaca.get_positions.return_value = [
            _equity("AAPL", 200), _short_option("AAPL260724C00340000")]
        self.engine._available_shares = Mock(return_value=(200, 200, 0))

        results, count = self.engine.execute_batch(
            [self._aapl_call(contracts=2)], self.put_seller,
            call_seller=self.call_seller)

        self.call_seller.execute_call_sale.assert_not_called()
        assert count == 0 and results[0]["success"] is False
        errors = self._errors("naked_call_invariant_blocked")
        assert (errors[0]["owned"], errors[0]["committed"],
                errors[0]["requested"]) == (200, 100, 200)

    def test_a_multi_contract_write_passes_when_the_shares_are_there(self):
        """F7, the other side: 300 owned - 100 committed backs two contracts."""
        self.alpaca.get_positions.return_value = [
            _equity("AAPL", 300), _short_option("AAPL260724C00340000")]

        results, count = self.engine.execute_batch(
            [self._aapl_call(contracts=2)], self.put_seller,
            call_seller=self.call_seller)

        self.call_seller.execute_call_sale.assert_called_once()
        assert count == 1 and results[0]["success"] is True
        assert self._errors("naked_call_invariant_blocked") == []


class TestSelectBatchDedupesOnTheOccRoot:
    """F8 — the per-batch keys are OCC roots, not equity spellings.

    Keyed on the raw symbol, `BRK.B` and `BRKB` are two underlyings to
    `select_batch`: each gets its own 100-share budget out of the same 100
    shares, and the one-position-per-underlying rule lets both through.
    """

    def setup_method(self):
        self.alpaca = Mock()
        self.engine = ExecutionEngine(self.alpaca, Mock(spec=Config))
        self.engine.logger = Mock()

    @staticmethod
    def _item(opp, collateral=0.0, premium=100.0, roi=0.0):
        return {"opportunity": opp, "collateral": collateral, "premium": premium,
                "roi": roi, "type": opp.get("type", "put")}

    def test_two_spellings_of_one_underlying_are_one_underlying(self):
        dotted = _brk_call(BRK_CALL_2)
        rooted = dict(_brk_call("BRKB260918C00470000"), symbol="BRKB")
        self.alpaca.get_positions.return_value = [_equity(BRK_EQUITY, 100)]

        selected, _ = self.engine.select_batch(
            [self._item(dotted), self._item(rooted)], 0.0)

        assert [o["option_symbol"] for o in selected] == [BRK_CALL_2]
        assert [d["reason"] for d in _drop_events(self.engine.logger)] == [
            "duplicate_underlying"]

    def test_the_share_ledger_is_one_budget_across_both_spellings(self):
        """Without the shared key each spelling would draw its own 100 shares
        from the same 100, and the second drop would never happen."""
        dotted = _brk_call(BRK_CALL_2)
        rooted = dict(_brk_call("BRKB260918C00470000"), symbol="BRKB")
        self.alpaca.get_positions.return_value = [
            _equity(BRK_EQUITY, 100), _short_option(BRK_CALL)]

        selected, _ = self.engine.select_batch(
            [self._item(dotted), self._item(rooted)], 0.0)

        assert selected == []
        assert [d["reason"] for d in _drop_events(self.engine.logger)] == [
            "insufficient_available_shares", "insufficient_available_shares"]

    def test_a_call_and_a_put_on_one_underlying_still_collapse(self):
        """The cross-pool rule survives the rekey."""
        call = _brk_call(BRK_CALL_2)
        put = {"symbol": "BRKB", "option_symbol": "BRKB260918P00400000",
               "type": "put", "strike_price": 400.0, "premium": 1.0,
               "contracts": 1}
        self.alpaca.get_positions.return_value = [_equity(BRK_EQUITY, 100)]

        selected, _ = self.engine.select_batch(
            [self._item(call), self._item(put, collateral=40000.0)], 100000.0)

        assert [o["option_symbol"] for o in selected] == [BRK_CALL_2]
        assert [d["reason"] for d in _drop_events(self.engine.logger)] == [
            "duplicate_underlying"]

    def test_the_selection_event_still_reports_the_equity_spelling(self):
        """The key is normalized; the telemetry is not. An operator greps for
        the symbol they hold."""
        self.alpaca.get_positions.return_value = [_equity(BRK_EQUITY, 100)]

        self.engine.select_batch([self._item(_brk_call(BRK_CALL_2))], 0.0)

        assert [e["symbol"] for e in _select_events(self.engine.logger)] == [
            BRK_EQUITY]


class TestJournalRecordsTheExecutingQuoteFC072:
    """N1: `options_wheel.trades` must record the book the order was PRICED off.

    `execute_batch` built its journal row from ``{**opp, "limit_price": ...}``.
    Since FC-072 the limit is priced off a quote refreshed at execution time,
    while ``opp`` still carries the :00 scan blob — so every row showed a
    :15 live-priced limit sitting next to a 15-minute-old bid/ask, and
    ``mid_price`` was NULL because no opportunity dict has ever had that key.
    Any "where did we price relative to the market" question — which is exactly
    the question the FC-072 two-week readout asks — read the wrong book.
    """

    BLOB = {"bid": 1.20, "ask": 1.40}
    LIVE = {"bid": 1.30, "ask": 1.50, "mid": 1.40}

    def setup_method(self):
        from src.strategy.execution_engine import clear_failed_symbols
        clear_failed_symbols()
        self.alpaca = Mock()
        self.alpaca.get_positions.return_value = [
            {"symbol": "AAPL", "qty": "500", "asset_class": "us_equity",
             "side": "long"}
        ]
        self.engine = ExecutionEngine(self.alpaca, Mock(spec=Config))
        self.engine.trade_journal = Mock()
        self.put_seller = Mock(spec=PutSeller)
        self.call_seller = Mock(spec=CallSeller)

    def _result(self, **kw):
        """The shape both sellers return post-FC-072."""
        result = {
            "success": True,
            "order_id": "o1",
            "limit_price": 1.40,
            "bid": self.LIVE["bid"],
            "ask": self.LIVE["ask"],
            "mid": self.LIVE["mid"],
            "quote_source": "live",
            "quote_age_s": 0.4,
            "tick_snapped": False,
        }
        result.update(kw)
        return result

    def _opportunity(self, option_symbol=PUT_SYM, **kw):
        opp = {
            "symbol": "AAPL",
            "option_symbol": option_symbol,
            "type": "call" if "C00" in option_symbol else "put",
            "contracts": 1,
            "premium": 1.30,
            "strike_price": 170,
            "shares_covered": 100,
            "stock_cost_basis": 150.0,
            "bid": self.BLOB["bid"],
            "ask": self.BLOB["ask"],
        }
        opp.update(kw)
        return opp

    def _row(self, opp=None, result=None, call=False):
        result = result or self._result()
        opp = opp or self._opportunity(CALL_SYM if call else PUT_SYM)
        if call:
            self.call_seller.execute_call_sale.return_value = result
        else:
            self.put_seller.execute_put_sale.return_value = result
        self.engine.execute_batch([opp], self.put_seller,
                                  call_seller=self.call_seller)
        assert self.engine.trade_journal.record_trade.call_count == 1
        return self.engine.trade_journal.record_trade.call_args.args[0]

    def test_the_row_carries_the_live_book_not_the_scan_blob(self):
        """THE regression. Pre-fix: bid 1.20 / ask 1.40 / mid_price None."""
        row = self._row()
        assert row["bid"] == 1.30
        assert row["ask"] == 1.50
        assert row["mid_price"] == 1.40
        assert row["limit_price"] == 1.40

    def test_the_call_leg_journals_the_same_way(self):
        row = self._row(call=True)
        assert (row["bid"], row["ask"], row["mid_price"]) == (1.30, 1.50, 1.40)

    def test_mid_price_is_no_longer_null(self):
        """No opportunity dict has ever carried `mid_price`, so the column was
        NULL on every row this engine has ever written."""
        assert self._row()["mid_price"] is not None

    def test_a_blob_priced_order_journals_the_blob_book_it_actually_used(self):
        """The point is provenance, not liveness: whatever priced the order is
        what gets recorded."""
        row = self._row(result=self._result(
            bid=self.BLOB["bid"], ask=self.BLOB["ask"], mid=1.30,
            quote_source="blob", quote_age_s=900.0, limit_price=1.30))
        assert (row["bid"], row["ask"], row["mid_price"]) == (1.20, 1.40, 1.30)

    def test_a_result_without_the_new_fields_degrades_to_the_scan_quote(self):
        """A producer that predates FC-072 must not write NULLs over a usable
        scan-time book."""
        row = self._row(result={"success": True, "order_id": "o1",
                                "limit_price": 1.24})
        assert (row["bid"], row["ask"]) == (1.20, 1.40)

    def test_the_opportunitys_other_fields_still_reach_the_row(self):
        """The merge must not have dropped everything `**opp` carried."""
        row = self._row()
        assert row["symbol"] == "AAPL"
        assert row["contracts"] == 1
        assert row["strike_price"] == 170
        assert row["status"] == "submitted"

    def test_quote_source_rides_the_event_and_gets_no_new_column(self):
        """Provenance is worth a log field, not a schema migration on the
        canonical trades table — it joins back by order_id."""
        with patch("src.strategy.execution_engine.log_system_event") as logged:
            self._row()
        executed = [c.kwargs for c in logged.call_args_list
                    if c.kwargs.get("event_type") == "trade_executed"]
        assert len(executed) == 1
        assert executed[0]["quote_source"] == "live"
        assert executed[0]["quote_age_s"] == 0.4
        assert executed[0]["tick_snapped"] is False

        row = self.engine.trade_journal.record_trade.call_args.args[0]
        assert "quote_source" not in TRADE_SCHEMA_FIELD_NAMES
        assert row.get("quote_source") is None
