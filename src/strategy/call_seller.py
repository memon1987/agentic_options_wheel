"""Call selling module for options wheel strategy."""

from typing import Dict, Any, Optional
from datetime import datetime
import structlog

from ..utils import clock
from ..api.alpaca_client import AlpacaClient
from ..api.market_data import MarketDataManager
from ..utils.config import Config
from ..utils.logging_events import log_trade_event, log_error_event
from ..utils.option_symbols import parse_option_symbol
from .cost_basis import (
    opportunity_floor_per_share,
    opportunity_shares_covered,
    opportunity_total_return_if_called,
)
from .limit_pricing import sell_limit_price

logger = structlog.get_logger(__name__)


class CallSeller:
    """Executes covered call trades and manages open call positions.

    FC-068 deleted this class's opportunity-*generation* half
    (``evaluate_covered_call_opportunity``, ``_calculate_call_position``,
    ``_resolve_cost_basis_floor`` and the ``CostBasisResolver`` they shared).
    Covered-call candidates come from
    :class:`~src.data.options_scanner.OptionsScanner` on the ``/scan`` path;
    what remains here is the ``/run`` execute leg (with the FC-050 execute-time
    floor read off the opportunity) and the monitor-cycle close checks.
    """

    def __init__(self, alpaca_client: AlpacaClient, market_data: MarketDataManager,
                 config: Config):
        """Initialize call seller.

        FC-068 removed the ``allow_bigquery_cost_basis`` keyword with the
        resolver it gated: ``execute_call_sale`` reads its floor from the
        opportunity via ``opportunity_floor_per_share`` and touches no
        resolver, so there is no BigQuery reader left on this class. The
        replay gate now lives on ``OptionsScanner``, the sole remaining
        producer.

        FC-069 item 8 (stage 1) removed the ``wheel_state_manager`` keyword
        and the ``set_active_call_details`` call it fed. Both were orphaned:
        ``/run`` and ``/monitor`` construct this class with three positional
        arguments and the replay mirrors them, so the state write fired
        nowhere. Stage 2 shrinks ``WheelStateManager`` itself.

        Args:
            alpaca_client: Alpaca API client
            market_data: Market data manager
            config: Configuration instance
        """
        self.alpaca = alpaca_client
        self.market_data = market_data
        self.config = config
        # FC-069 item 6 deleted `_entry_times` and the min-hold gate it fed.
        # CallSeller is constructed per request, so the dict populated during
        # `/run` was already gone by `/monitor` — the gate has been open since
        # inception. Hold discipline lives in the DTE profit bands.

    def execute_call_sale(self, opportunity: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Execute a covered call trade.

        Args:
            opportunity: Call opportunity details

        Returns:
            Trade execution result
        """
        try:
            # SAFETY (FC-048): Reject put options that were incorrectly routed
            # here. Mirrors put_seller.execute_put_sale's guard -- the put side
            # has had this protection since FC-021 and the call side did not,
            # which is the same asymmetry that let the misroute go unnoticed.
            option_symbol = opportunity.get('option_symbol', '')
            parsed = parse_option_symbol(option_symbol)
            if parsed.get('option_type') == 'put':
                logger.warning("Put option incorrectly routed to call_seller - rejecting",
                              event_category="risk",
                              event_type="put_rejected_by_call_seller",
                              symbol=option_symbol,
                              underlying=parsed.get('underlying'))
                return {
                    'success': False,
                    'error_type': 'wrong_seller',
                    'message': f'Put option {option_symbol} cannot be executed by call_seller',
                    'non_retryable': True
                }

            # DEFENSIVE: Final validation before execution - prevent guaranteed losses.
            # FC-050: the floor is read through opportunity_floor_per_share, which
            # understands both producers' shapes. Reading only the wheel-engine keys
            # meant this check never fired on scanner-produced opportunities — the
            # only kind production executes.
            strike_price = opportunity.get('strike_price', 0)
            cost_basis_per_share = opportunity_floor_per_share(opportunity)

            # FC-050: no resolvable floor is a blocking condition, not a reason to
            # skip the check. Post-FC-050 every scanner opportunity carries a
            # positive cost_basis_per_share and the wheel engine always sets
            # stock_cost_basis, so this means a malformed opportunity — the exact
            # case that must not reach the broker. Mirrors the FC-029
            # cost_basis_floor_unresolved block on the evaluation path.
            if cost_basis_per_share <= 0:
                log_error_event(
                    logger,
                    error_type="cost_basis_floor_unresolved_at_execution",
                    error_message=(
                        f"No cost-basis floor on opportunity for "
                        f"{opportunity.get('option_symbol', 'unknown')} "
                        f"({opportunity.get('symbol', 'unknown')}) - blocking call write"
                    ),
                    component="call_seller",
                    recoverable=True,
                    symbol=opportunity.get('symbol', ''),
                    option_symbol=opportunity.get('option_symbol', ''),
                    strike_price=strike_price,
                )
                return {
                    'success': False,
                    'error_type': 'cost_basis_floor_unresolved_at_execution',
                    'message': (
                        f"No cost-basis floor on opportunity for "
                        f"{opportunity.get('option_symbol', 'unknown')} - call not written"
                    ),
                    'non_retryable': False
                }

            # A missing/zero strike also lands here rather than skipping the
            # check: with a real floor in hand, "no strike" is malformed input,
            # not a reason to trade unprotected.
            if strike_price < cost_basis_per_share:
                log_error_event(
                    logger,
                    error_type="call_below_cost_basis_blocked",
                    error_message=f"Strike ${strike_price} below cost basis ${cost_basis_per_share:.2f}/share - guaranteed loss prevented",
                    component="call_seller",
                    recoverable=True,
                    symbol=opportunity.get('symbol', ''),
                    option_symbol=opportunity.get('option_symbol', ''),
                    strike_price=strike_price,
                    cost_basis_per_share=cost_basis_per_share,
                    loss_per_share=cost_basis_per_share - strike_price
                )
                return {
                    'success': False,
                    'error': 'strike_below_cost_basis',
                    'message': f'Strike ${strike_price} below cost basis ${cost_basis_per_share:.2f}/share'
                }

            option_symbol = opportunity['option_symbol']
            contracts = opportunity['contracts']
            premium = opportunity['premium']
            
            # Calculate limit price (FC-072). This used to be an unconditional
            # `round(premium * 0.95, 2)` — 5% below mid on every covered-call
            # write, whether or not the book supported it. Measured over the
            # trade journal that discount ran the call leg 5.0% below mid while
            # the put leg ran 1.0% ABOVE it, an unstated Symmetry violation
            # worth up to ~$2.3k of forgone premium across 138 fills.
            #
            # Now: price at `mid + f * spread` on a usable quote, where `f` is
            # `strategy.call_limit_spread_fraction` (default 0.0 -> exactly
            # mid). The put leg's +10%-of-spread bias is deliberately NOT
            # ported here in this change: the same measurement showed calls
            # filling 87% priced below mid against puts at 73% priced above it,
            # so the aggression is a second, separately-measured step the
            # operator flips via config once fill data exists.
            #
            # The fallback (`mid * 0.95`) is unchanged and fires on a missing,
            # crossed, or stale quote — see src/strategy/limit_pricing.py for
            # the shared predicate and the spread sanity cap, which both legs
            # now use so they cannot drift.
            bid = opportunity.get('bid')
            ask = opportunity.get('ask')
            limit_price, spread, applied_spread_fraction = sell_limit_price(
                mid=premium,
                bid=bid,
                ask=ask,
                spread_fraction=self.config.call_limit_spread_fraction,
            )

            logger.info("Executing covered call sale",
                       event_category="trade",
                       event_type="call_sale_executing",
                       symbol=option_symbol,
                       contracts=contracts,
                       premium=premium,
                       bid=bid,
                       ask=ask,
                       spread=round(spread, 2) if spread is not None else None,
                       # The fraction ACTUALLY applied: None means the fallback
                       # fired, so a fill-rate study can separate priced-at-mid
                       # writes from degraded-quote writes without a second
                       # change.
                       spread_fraction=applied_spread_fraction,
                       limit_price=limit_price)
            
            # Place the sell order
            order_result = self.alpaca.place_option_order(
                symbol=option_symbol,
                qty=contracts,
                side='sell',
                order_type='limit',
                limit_price=limit_price
            )

            # Handle order result (success or structured error)
            if order_result and order_result.get('success', True):
                result = {
                    'success': True,
                    'order_id': order_result.get('order_id'),
                    'symbol': option_symbol,
                    'contracts': contracts,
                    'limit_price': limit_price,
                    'strategy': 'sell_call',
                    'timestamp': clock.now().isoformat()
                }

                # Enhanced logging for BigQuery analytics.
                #
                # FC-065 Phase 4: these three economics fields used to read
                # `shares_covered` / `stock_cost_basis` / `total_return_if_called`
                # straight off the opportunity. Those are wheel-engine-only
                # keys, and production runs the scanner path, so EVERY live
                # `call_sale_executed` logged 0/0/0 — the fourth instance of
                # the untyped-shape root cause and the reason FC-029's
                # production-validation item could not be satisfied. They now
                # go through the same shape-tolerant accessors FC-050
                # introduced for the floor itself.
                shares_covered = opportunity_shares_covered(opportunity)
                log_trade_event(
                    logger,
                    event_type="call_sale_executed",
                    symbol=option_symbol,
                    underlying=opportunity.get('symbol', ''),
                    strategy="sell_call",
                    success=True,
                    strike_price=opportunity.get('strike_price', 0),
                    premium=premium,
                    contracts=contracts,
                    limit_price=limit_price,
                    order_id=order_result.get('order_id'),
                    shares_covered=shares_covered,
                    # Total basis of the covered shares, so the field keeps its
                    # documented (wheel-engine) units while being derived from
                    # the per-share floor the gate actually enforced above.
                    stock_cost_basis=round(cost_basis_per_share * shares_covered, 2),
                    cost_basis_per_share=cost_basis_per_share,
                    total_return_if_called=round(
                        opportunity_total_return_if_called(opportunity), 2),
                    dte=opportunity.get('dte', 0)
                )

                return result
            else:
                # Order failed - return structured error
                error_type = order_result.get('error_type', 'unknown') if order_result else 'unknown'
                error_msg = order_result.get('error_message', 'Unknown error') if order_result else 'No result returned'

                # Enhanced error logging
                log_error_event(
                    logger,
                    error_type=error_type,
                    error_message=error_msg,
                    component="call_seller",
                    recoverable=True,
                    symbol=option_symbol,
                    underlying=opportunity.get('symbol', ''),
                    strike_price=opportunity.get('strike_price', 0),
                    contracts=contracts,
                    limit_price=limit_price
                )

                # Return structured error dict instead of None for consistent return type
                return {
                    'success': False,
                    'error': error_type,
                    'message': error_msg,
                    'symbol': option_symbol,
                    'strategy': 'sell_call',
                    'timestamp': clock.now().isoformat()
                }

        except Exception as e:
            # Enhanced error logging
            log_error_event(
                logger,
                error_type="execution_exception",
                error_message=str(e),
                component="call_seller",
                recoverable=False,
                symbol=opportunity.get('option_symbol', ''),
                underlying=opportunity.get('symbol', '')
            )
            # Return structured error dict instead of None for consistent return type
            return {
                'success': False,
                'error': 'execution_exception',
                'message': str(e),
                'symbol': opportunity.get('option_symbol', ''),
                'strategy': 'sell_call',
                'timestamp': clock.now().isoformat()
            }
    
    def _parse_dte_from_option_symbol(self, option_symbol: str) -> int:
        """
        Extract DTE (Days to Expiration) from option symbol.

        Option symbol format: AMD251031C00350000
                              ^^^YYMMDD = 251031 = 2025-10-31

        Args:
            option_symbol: Option symbol string

        Returns:
            Days to expiration (0-N), or 7 as fallback if parse fails
        """
        try:
            import re
            from datetime import datetime, timezone

            # Extract date portion (6 digits after ticker, before P/C)
            match = re.search(r'(\d{6})[PC]', option_symbol)
            if not match:
                logger.warning("Could not parse expiration date from option symbol",
                              event_category="data",
                              event_type="option_symbol_parse_warning",
                              symbol=option_symbol)
                return 7  # Default fallback

            date_str = match.group(1)

            # Parse YYMMDD format
            year = 2000 + int(date_str[0:2])
            month = int(date_str[2:4])
            day = int(date_str[4:6])

            exp_date = datetime(year, month, day, tzinfo=timezone.utc)
            now = clock.now_utc()

            dte = (exp_date.date() - now.date()).days

            return max(0, dte)  # Never negative

        except Exception as e:
            logger.error("Failed to parse DTE from option symbol",
                        event_category="error",
                        event_type="dte_parse_error",
                        symbol=option_symbol,
                        error=str(e))
            return 7  # Default fallback

    def _get_profit_target_for_dte(self, dte: int) -> float:
        """
        Get profit target based on DTE using configured bands.

        Args:
            dte: Days to expiration

        Returns:
            Profit target percentage (0.0-1.0)
        """
        # If dynamic profit targets disabled, use static target
        if not self.config.use_dynamic_profit_target:
            return self.config.profit_taking_static_target

        # Find matching DTE band
        for band in self.config.profit_taking_dte_bands:
            if band['dte'] == dte:
                target = band['profit_target']

                logger.debug("Using DTE band profit target",
                            event_category="trade",
                            event_type="profit_target_dte_band",
                            dte=dte,
                            target=f"{target*100:.0f}%",
                            description=band.get('description', ''))

                # Apply safety bounds
                return max(
                    self.config.profit_taking_min_target,
                    min(target, self.config.profit_taking_max_target)
                )

        # No exact match - check if long-dated (DTE > 7)
        if dte > 7:
            target = self.config.profit_taking_default_long_dte
            logger.debug("Using long DTE default profit target",
                        event_category="trade",
                        event_type="profit_target_long_dte",
                        dte=dte,
                        target=f"{target*100:.0f}%")
            return target

        # Fallback to static target
        logger.warning("No DTE band found, using static profit target",
                      event_category="trade",
                      event_type="profit_target_static_fallback",
                      dte=dte,
                      static_target=f"{self.config.profit_taking_static_target*100:.0f}%")
        return self.config.profit_taking_static_target

    def should_close_call_early(self, call_position: Dict[str, Any], current_option_data: Dict[str, Any] = None) -> bool:
        """Determine if a short call should be closed early for profit or stop loss.

        Uses dynamic DTE-based profit targets optimized for theta decay curves.

        Args:
            call_position: Short call position details
            current_option_data: Current option market data with delta, etc. (unused in Phase 1)

        Returns:
            True if position should be closed early
        """
        try:
            unrealized_pl = float(call_position['unrealized_pl'])
            position_value = abs(float(call_position['market_value']))

            option_symbol = call_position['symbol']

            # Profit target: Dynamic based on DTE (theta-optimized)
            if unrealized_pl > 0 and position_value > 0:
                profit_percentage = unrealized_pl / position_value

                # Get dynamic profit target based on DTE
                dte = self._parse_dte_from_option_symbol(option_symbol)
                profit_target = self._get_profit_target_for_dte(dte)

                if profit_percentage >= profit_target:
                    logger.info("Call position reached dynamic profit target",
                               event_category="trade",
                               event_type="call_profit_target_reached",
                               symbol=option_symbol,
                               dte=dte,
                               profit_pct=round(profit_percentage * 100, 1),
                               target_pct=round(profit_target * 100, 1))
                    return True

            # Stop loss logic for short-term options (enabled for calls)
            if self.config.use_call_stop_loss and unrealized_pl < 0:
                return self._check_call_stop_loss(call_position, current_option_data)

            return False

        except Exception as e:
            logger.error("Failed to evaluate early close",
                        event_category="error",
                        event_type="early_close_evaluation_error",
                        error=str(e))
            return False

    def _check_call_stop_loss(self, call_position: Dict[str, Any], current_option_data: Dict[str, Any] = None) -> bool:
        """Check if call position should be closed due to stop loss.
        
        Args:
            call_position: Call position details
            current_option_data: Current option market data
            
        Returns:
            True if stop loss should be triggered
        """
        try:
            unrealized_pl = float(call_position['unrealized_pl'])
            position_value = abs(float(call_position['market_value']))
            
            # Method 1: Traditional premium-based stop loss (adjusted for time decay)
            if position_value > 0:
                loss_percentage = abs(unrealized_pl) / position_value
                
                # For short-term options, use a higher threshold since time decay is expected
                stop_loss_threshold = self.config.call_stop_loss_percent * self.config.stop_loss_multiplier
                
                if loss_percentage >= stop_loss_threshold:
                    logger.warning("Call position hit premium stop loss",
                                  event_category="risk",
                                  event_type="call_premium_stop_loss",
                                  symbol=call_position['symbol'],
                                  loss_pct=loss_percentage,
                                  threshold=stop_loss_threshold)
                    return True
            
            # Method 2: Delta-based stop loss (if current option data available)
            if current_option_data and 'delta' in current_option_data:
                current_delta = abs(current_option_data['delta'])
                
                # If delta > 0.5, the call is likely ITM and stock moved significantly up
                if current_delta > 0.5:  # Delta > 0.5 means we're likely ITM
                    logger.warning("Call position hit delta stop loss",
                                  event_category="risk",
                                  event_type="call_delta_stop_loss",
                                  symbol=call_position['symbol'],
                                  current_delta=current_delta)
                    return True
            
            return False
            
        except Exception as e:
            logger.error("Failed to check call stop loss",
                        event_category="error",
                        event_type="call_stop_loss_check_error",
                        error=str(e))
            return False
