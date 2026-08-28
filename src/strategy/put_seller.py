"""Put selling module for options wheel strategy."""

from typing import Dict, Any, Optional
from datetime import datetime
import structlog

from ..utils import clock
from ..api.alpaca_client import AlpacaClient
from ..api.market_data import MarketDataManager
from ..utils.config import Config
from ..utils.logging_events import log_trade_event, log_error_event
from ..utils.option_symbols import parse_option_symbol
from .limit_pricing import sell_limit_price

logger = structlog.get_logger(__name__)

# Fraction of the bid/ask spread the put leg adds to mid when the quote is
# usable — biased toward the ask for premium collection. Historical value,
# unchanged by FC-072 and deliberately not a config key (see execute_put_sale).
PUT_LIMIT_SPREAD_FRACTION = 0.10


class PutSeller:
    """Handles cash-secured put selling for the wheel strategy."""

    def __init__(self, alpaca_client: AlpacaClient, market_data: MarketDataManager, config: Config):
        """Initialize put seller.

        Args:
            alpaca_client: Alpaca API client
            market_data: Market data manager
            config: Configuration instance
        """
        self.alpaca = alpaca_client
        self.market_data = market_data
        self.config = config
        # FC-069 item 6 deleted `_entry_times` and the min-hold gate it fed.
        # PutSeller is constructed per request, so the dict populated during
        # `/run` was already gone by `/monitor` — the gate has been open since
        # inception. Hold discipline lives in the DTE profit bands.

    def _calculate_position_size(self, put_option: Dict[str, Any], override_buying_power: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Calculate appropriate position size for put selling.

        Args:
            put_option: Put option details
            override_buying_power: Optional buying power override (for tracking during execution loop)

        Returns:
            Position sizing details or None if invalid
        """
        try:
            # Get account info
            account_info = self.alpaca.get_account()
            portfolio_value = float(account_info['portfolio_value'])

            # Use override if provided (for local tracking during execution),
            # otherwise get from Alpaca API
            if override_buying_power is not None:
                buying_power = override_buying_power
            else:
                # Use options_buying_power for options trading (more accurate for options margin)
                # Fall back to regular buying_power if not available
                buying_power = float(account_info.get('options_buying_power') or account_info['buying_power'])
            
            # Calculate maximum position size based on portfolio allocation
            max_position_value = portfolio_value * self.config.max_position_size
            
            # Calculate capital required per contract (strike price * 100)
            strike_price = put_option['strike_price']
            capital_per_contract = strike_price * 100
            
            # Calculate maximum contracts based on position size limit
            max_contracts_by_position = int(max_position_value // capital_per_contract)
            
            # Calculate maximum contracts based on available buying power
            max_contracts_by_buying_power = int(buying_power // capital_per_contract)
            
            # Take the minimum to ensure we don't exceed limits
            max_contracts = min(max_contracts_by_position, max_contracts_by_buying_power, 10)  # Cap at 10 contracts

            logger.info("STAGE 8: Position sizing calculation",
                       event_category="filtering",
                       event_type="stage_8_calculation",
                       symbol=put_option['symbol'],
                       strike=strike_price,
                       portfolio_value=portfolio_value,
                       buying_power=buying_power,
                       capital_per_contract=capital_per_contract,
                       max_by_position=max_contracts_by_position,
                       max_by_buying_power=max_contracts_by_buying_power,
                       max_contracts_allowed=max_contracts)

            if max_contracts <= 0:
                logger.warning("STAGE 8 BLOCKED: Position sizing - insufficient capital",
                              event_category="filtering",
                              event_type="stage_8_blocked",
                              strike=strike_price,
                              capital_per_contract=capital_per_contract,
                              buying_power=buying_power,
                              reason="max_contracts_zero")
                return None

            # Conservative sizing - start with 1 contract for new positions
            contracts = 1
            
            # Calculate metrics
            capital_required = contracts * capital_per_contract
            premium_per_contract = put_option['mid_price']
            max_profit = contracts * premium_per_contract * 100  # Premium collected
            breakeven = strike_price - premium_per_contract
            
            # Validate against risk limits
            portfolio_allocation = capital_required / portfolio_value
            if portfolio_allocation > self.config.max_position_size:
                logger.warning("STAGE 8 BLOCKED: Position size exceeds allocation limit",
                              event_category="filtering",
                              event_type="stage_8_blocked",
                              allocation=portfolio_allocation,
                              max_allowed=self.config.max_position_size,
                              reason="portfolio_allocation_exceeded")
                return None

            logger.info("STAGE 8 PASSED: Position sizing approved",
                       event_category="filtering",
                       event_type="stage_8_passed",
                       symbol=put_option['symbol'],
                       contracts=contracts,
                       capital_required=capital_required,
                       portfolio_allocation=round(portfolio_allocation, 3),
                       max_profit=max_profit)

            return {
                'contracts': contracts,
                'capital_required': capital_required,
                'max_profit': max_profit,
                'breakeven': breakeven,
                'portfolio_allocation': portfolio_allocation,
                'premium_per_contract': premium_per_contract
            }
            
        except Exception as e:
            logger.error("Failed to calculate position size",
                        event_category="error",
                        event_type="position_size_calculation_error",
                        error=str(e))
            return None
    
    def execute_put_sale(self, opportunity: Dict[str, Any], skip_buying_power_check: bool = False) -> Optional[Dict[str, Any]]:
        """Execute a put selling trade.

        Args:
            opportunity: Put opportunity details

        Returns:
            Trade execution result
        """
        try:
            option_symbol = opportunity['option_symbol']
            contracts = opportunity['contracts']
            premium = opportunity['premium']
            strike_price = opportunity.get('strike_price', 0)

            # SAFETY: Reject call options that were incorrectly routed here.
            # Put seller must ONLY handle puts. Calls require share verification
            # and must go through call_seller.execute_call_sale().
            parsed = parse_option_symbol(option_symbol)
            if parsed.get('option_type') == 'call':
                logger.warning("Call option incorrectly routed to put_seller — rejecting",
                              event_category="risk",
                              event_type="call_rejected_by_put_seller",
                              symbol=option_symbol,
                              underlying=parsed.get('underlying'))
                return {
                    'success': False,
                    'error_type': 'wrong_seller',
                    'message': f'Call option {option_symbol} cannot be executed by put_seller',
                    'non_retryable': True
                }

            # CRITICAL: Validate buying power before placing order
            collateral_required = strike_price * 100 * contracts  # Cash-secured put

            # Skip buying power check if using local tracking (Alpaca API returns stale $0 after trades)
            if not skip_buying_power_check:
                try:
                    account = self.alpaca.get_account()
                    available_bp = account.get('options_buying_power', 0)

                    if collateral_required > available_bp:
                        # Enhanced error logging for BigQuery analytics
                        log_error_event(
                            logger,
                            error_type="insufficient_buying_power",
                            error_message=f'Need ${collateral_required:,.2f} but only ${available_bp:,.2f} available',
                            component="put_seller",
                            recoverable=True,
                            symbol=option_symbol,
                            underlying=opportunity.get('symbol', ''),
                            required=collateral_required,
                            available=available_bp,
                            shortage=collateral_required - available_bp
                        )
                        return {
                            'success': False,
                            'error': 'insufficient_buying_power',
                            'message': f'Need ${collateral_required:,.2f} but only ${available_bp:,.2f} available',
                            'symbol': option_symbol,
                            'timestamp': clock.now().isoformat()
                        }

                    logger.info("Buying power check passed",
                               event_category="trade",
                               event_type="buying_power_validated",
                               required=collateral_required,
                               available=available_bp,
                               margin=available_bp - collateral_required)

                except Exception as bp_error:
                    # Don't silently continue - return structured error
                    log_error_event(
                        logger,
                        error_type="buying_power_check_failed",
                        error_message=str(bp_error),
                        component="put_seller",
                        recoverable=True,
                        symbol=option_symbol,
                        collateral_required=collateral_required
                    )
                    return {
                        'success': False,
                        'error': 'buying_power_check_failed',
                        'message': f'Could not validate buying power: {str(bp_error)}',
                        'symbol': option_symbol,
                        'strategy': 'sell_put',
                        'timestamp': clock.now().isoformat()
                    }

            # Calculate limit price: mid + 10% of spread (biased toward ask for
            # premium collection). The formula is unchanged by FC-072; what is
            # new is the shared usability predicate underneath it — in
            # particular the spread sanity cap (a book wider than half of mid
            # is stale or crossed, and its midpoint is noise). Before FC-072
            # this leg would happily price off a crossed quote. A normal quote
            # prices exactly as it did.
            #
            # PUT_LIMIT_SPREAD_FRACTION stays hardcoded on purpose: FC-072 gave
            # the CALL leg a config knob because that leg is the one being
            # moved and measured. Making both legs configurable in the same
            # change would put two variables in flight at once.
            bid = opportunity.get('bid')
            ask = opportunity.get('ask')
            limit_price, spread, applied_spread_fraction = sell_limit_price(
                mid=premium,
                bid=bid,
                ask=ask,
                spread_fraction=PUT_LIMIT_SPREAD_FRACTION,
            )

            logger.info("Executing put sale",
                       event_category="trade",
                       event_type="put_sale_executing",
                       symbol=option_symbol,
                       contracts=contracts,
                       premium=premium,
                       bid=bid,
                       ask=ask,
                       spread=round(spread, 2) if spread is not None else None,
                       # None when the fallback fired — see the call leg.
                       spread_fraction=applied_spread_fraction,
                       limit_price=limit_price,
                       collateral_required=collateral_required)

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
                # Order placed successfully
                result = {
                    'success': True,
                    'order_id': order_result.get('order_id'),
                    'symbol': option_symbol,
                    'contracts': contracts,
                    'limit_price': limit_price,
                    'strategy': 'sell_put',
                    'timestamp': clock.now().isoformat()
                }

                # Enhanced logging for BigQuery analytics
                log_trade_event(
                    logger,
                    event_type="put_sale_executed",
                    symbol=option_symbol,
                    underlying=opportunity.get('symbol', ''),
                    strategy="sell_put",
                    success=True,
                    strike_price=strike_price,
                    premium=premium,
                    contracts=contracts,
                    limit_price=limit_price,
                    order_id=order_result.get('order_id'),
                    collateral_required=collateral_required,
                    dte=opportunity.get('dte', 0)
                )

                return result

            else:
                # Order failed - return structured error
                error_type = order_result.get('error_type', 'order_error') if order_result else 'order_error'
                error_msg = order_result.get('error_message', 'Unknown error') if order_result else 'No result returned'
                non_retryable = order_result.get('non_retryable', False) if order_result else False

                # Enhanced error logging for BigQuery analytics
                log_error_event(
                    logger,
                    error_type=error_type,
                    error_message=error_msg,
                    component="put_seller",
                    recoverable=not non_retryable,
                    symbol=option_symbol,
                    underlying=opportunity.get('symbol', ''),
                    strike_price=strike_price,
                    contracts=contracts,
                    limit_price=limit_price
                )

                return {
                    'success': False,
                    'error': error_type,
                    'message': error_msg,
                    'non_retryable': non_retryable,
                    'symbol': option_symbol,
                    'strategy': 'sell_put',
                    'timestamp': clock.now().isoformat()
                }
                
        except Exception as e:
            # Enhanced error logging for BigQuery analytics
            log_error_event(
                logger,
                error_type="execution_exception",
                error_message=str(e),
                component="put_seller",
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
                'strategy': 'sell_put',
                'timestamp': clock.now().isoformat()
            }
    
    def _parse_dte_from_option_symbol(self, option_symbol: str) -> int:
        """
        Extract DTE (Days to Expiration) from option symbol.

        Delegates to the shared parse_option_symbol() utility.

        Args:
            option_symbol: Option symbol string

        Returns:
            Days to expiration (0-N), or 7 as fallback if parse fails
        """
        parsed = parse_option_symbol(option_symbol)
        dte = parsed.get('dte', 0)
        # If parsing failed entirely (no expiration found), use fallback
        if parsed.get('expiration_date') is None:
            return 7
        return dte

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

    def should_close_put_early(self, put_position: Dict[str, Any], current_option_data: Dict[str, Any] = None) -> bool:
        """Determine if a short put should be closed early for profit or stop loss.

        Uses dynamic DTE-based profit targets optimized for theta decay curves.

        Args:
            put_position: Short put position details
            current_option_data: Current option market data with delta, etc. (unused in Phase 1)

        Returns:
            True if position should be closed early
        """
        try:
            unrealized_pl = float(put_position['unrealized_pl'])
            position_value = abs(float(put_position['market_value']))

            option_symbol = put_position['symbol']

            # Profit target: Dynamic based on DTE (theta-optimized)
            if unrealized_pl > 0 and position_value > 0:
                profit_percentage = unrealized_pl / position_value

                # Get dynamic profit target based on DTE
                dte = self._parse_dte_from_option_symbol(option_symbol)
                profit_target = self._get_profit_target_for_dte(dte)

                if profit_percentage >= profit_target:
                    logger.info("Put position reached dynamic profit target",
                               event_category="trade",
                               event_type="put_profit_target_reached",
                               symbol=option_symbol,
                               dte=dte,
                               profit_pct=round(profit_percentage * 100, 1),
                               target_pct=round(profit_target * 100, 1))
                    return True

            # Stop loss logic for short-term options (disabled by default for puts)
            if self.config.use_put_stop_loss and unrealized_pl < 0:
                return self._check_put_stop_loss(put_position, current_option_data)

            return False

        except Exception as e:
            logger.error("Failed to evaluate early close",
                        event_category="error",
                        event_type="early_close_evaluation_error",
                        error=str(e))
            return False

    def _check_put_stop_loss(self, put_position: Dict[str, Any], current_option_data: Dict[str, Any] = None) -> bool:
        """Check if put position should be closed due to stop loss.
        
        Args:
            put_position: Put position details
            current_option_data: Current option market data
            
        Returns:
            True if stop loss should be triggered
        """
        try:
            unrealized_pl = float(put_position['unrealized_pl'])
            position_value = abs(float(put_position['market_value']))
            
            # Method 1: Traditional premium-based stop loss (adjusted for time decay)
            if position_value > 0:
                loss_percentage = abs(unrealized_pl) / position_value
                
                # For short-term options, use a higher threshold since time decay is expected
                stop_loss_threshold = self.config.put_stop_loss_percent * self.config.stop_loss_multiplier
                
                if loss_percentage >= stop_loss_threshold:
                    logger.warning("Put position hit premium stop loss",
                                  event_category="risk",
                                  event_type="put_premium_stop_loss",
                                  symbol=put_position['symbol'],
                                  loss_pct=loss_percentage,
                                  threshold=stop_loss_threshold)
                    return True
            
            # Method 2: Delta-based stop loss (if current option data available)
            if current_option_data and 'delta' in current_option_data:
                current_delta = abs(current_option_data['delta'])
                
                # If delta has doubled, the underlying has moved significantly against us
                # This is more reliable than premium-based for short-term options
                if current_delta > 0.5:  # Delta > 0.5 means we're likely ITM
                    logger.warning("Put position hit delta stop loss",
                                  event_category="risk",
                                  event_type="put_delta_stop_loss",
                                  symbol=put_position['symbol'],
                                  current_delta=current_delta)
                    return True
            
            # Method 3: Intrinsic value stop loss (if we can calculate it)
            # This would require current stock price and strike price parsing
            # For now, rely on methods 1 and 2
            
            return False
            
        except Exception as e:
            logger.error("Failed to check put stop loss",
                        event_category="error",
                        event_type="put_stop_loss_check_error",
                        error=str(e))
            return False