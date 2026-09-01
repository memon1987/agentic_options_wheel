"""Market data utilities for options wheel strategy."""

from typing import List, Dict, Any, Optional, Tuple
from datetime import date, datetime, timedelta
import pandas as pd
import numpy as np
import structlog

from .alpaca_client import AlpacaClient
from ..utils import clock
from ..utils.config import Config
from ..utils.option_symbols import coerce_expiry_date

logger = structlog.get_logger(__name__)

# Default cache TTL (5 minutes).
#
# Ages are measured with clock.now(), not time.time(). In production the two
# are identical. During a backtest replay they are not: hundreds of simulated
# days pass within a few seconds of wall time, so a wall-clock TTL never
# expires and the strategy is served day one's option chain forever — it will
# happily propose a contract that expired weeks ago. Measuring age in
# simulated time makes the cache expire between simulated days, as intended.
_DEFAULT_CACHE_TTL = timedelta(seconds=300)


# FC-078 moved the body to ``src/utils/option_symbols.coerce_expiry_date`` so
# ``RiskManager.validate_roll``'s roll-horizon bound can share the exact same
# coercion. The name is kept as a module-local alias: every existing call site
# and test reference resolves unchanged.
_coerce_date = coerce_expiry_date


class MarketDataManager:
    """Manager for market data operations and analysis."""

    def __init__(self, alpaca_client: AlpacaClient, config: Config):
        """Initialize market data manager.

        Args:
            alpaca_client: Alpaca client instance
            config: Configuration instance
        """
        self.alpaca = alpaca_client
        self.config = config
        # Caches: dict of symbol -> (value, timestamp)
        self._metrics_cache: Dict[str, Tuple[Dict[str, Any], datetime]] = {}
        self._options_chain_cache: Dict[str, Tuple[Dict[str, List[Dict[str, Any]]], datetime]] = {}
        self._cache_ttl = _DEFAULT_CACHE_TTL
        # FC-065 Phase 4: last call-chain rejection breakdown per symbol, so a
        # `no_candidates` decision record can say WHICH filter emptied the
        # chain. Read-only side channel — `find_suitable_calls` keeps its
        # List[...] return type, so no caller or test has to change shape.
        self.last_call_rejection_stats: Dict[str, Dict[str, int]] = {}
        
    def get_stock_metrics(self, symbol: str) -> Dict[str, Any]:
        """Get comprehensive stock metrics for wheel strategy evaluation.

        Results are cached for up to 5 minutes to avoid repeated API calls
        for the same symbol within a scan cycle.

        Args:
            symbol: Stock symbol

        Returns:
            Dictionary with stock metrics
        """
        try:
            # Check cache first
            cached = self._metrics_cache.get(symbol)
            if cached is not None:
                value, ts = cached
                if clock.now() - ts < self._cache_ttl:
                    logger.debug("Using cached stock metrics",
                                event_category="data",
                                event_type="stock_metrics_cache_hit",
                                symbol=symbol)
                    return value
            # Get current quote
            quote = self.alpaca.get_stock_quote(symbol)
            current_price = (quote['bid'] + quote['ask']) / 2
            
            # Get historical data for metrics calculation
            bars = self.alpaca.get_stock_bars(symbol, days=30)
            
            if bars.empty:
                logger.warning("No historical data available",
                              event_category="data",
                              event_type="no_historical_data",
                              symbol=symbol)
                return {}
            
            # Calculate metrics
            avg_volume = bars['volume'].mean()
            price_volatility = bars['close'].pct_change().std() * np.sqrt(252)  # Annualized
            
            # Price criteria check
            meets_price_criteria = (
                self.config.min_stock_price <= current_price <= self.config.max_stock_price
            )
            
            # Volume criteria check
            meets_volume_criteria = avg_volume >= self.config.min_avg_volume
            
            result = {
                'symbol': symbol,
                'current_price': current_price,
                'bid': quote['bid'],
                'ask': quote['ask'],
                'bid_ask_spread': quote['ask'] - quote['bid'],
                'avg_volume': avg_volume,
                'price_volatility': price_volatility,
                'meets_price_criteria': meets_price_criteria,
                'meets_volume_criteria': meets_volume_criteria,
                'suitable_for_wheel': meets_price_criteria and meets_volume_criteria
            }
            self._metrics_cache[symbol] = (result, clock.now())
            return result

        except Exception as e:
            logger.error("Failed to get stock metrics",
                        event_category="error",
                        event_type="stock_metrics_error",
                        symbol=symbol,
                        error=str(e))
            return {}
    
    def filter_suitable_stocks(self, symbols: List[str]) -> List[Dict[str, Any]]:
        """Filter stocks suitable for wheel strategy.

        Args:
            symbols: List of stock symbols to evaluate

        Returns:
            List of suitable stocks with metrics
        """
        suitable_stocks = []
        rejected_stocks = []

        for symbol in symbols:
            metrics = self.get_stock_metrics(symbol)
            if metrics and metrics.get('suitable_for_wheel', False):
                suitable_stocks.append(metrics)
                logger.info("Stock passed price/volume filter",
                           event_category="filtering",
                           event_type="stock_passed_filter",
                           symbol=symbol,
                           price=metrics['current_price'],
                           avg_volume=int(metrics['avg_volume']),
                           volatility=round(metrics['price_volatility'], 3))
            elif metrics:
                # Log rejection reason
                rejection_reasons = []
                if not metrics.get('meets_price_criteria'):
                    rejection_reasons.append(f"price ${metrics['current_price']:.2f} outside ${self.config.min_stock_price}-${self.config.max_stock_price}")
                if not metrics.get('meets_volume_criteria'):
                    rejection_reasons.append(f"volume {int(metrics['avg_volume']):,} < {self.config.min_avg_volume:,}")

                logger.info("Stock rejected by price/volume filter",
                           event_category="filtering",
                           event_type="stock_rejected_filter",
                           symbol=symbol,
                           reasons=rejection_reasons,
                           price=metrics['current_price'],
                           avg_volume=int(metrics['avg_volume']))
                rejected_stocks.append(symbol)

        # Sort by liquidity (volume) and low volatility preference
        suitable_stocks.sort(key=lambda x: (-x['avg_volume'], x['price_volatility']))

        logger.info("STAGE 1 COMPLETE: Price/Volume filtering",
                   event_category="filtering",
                   event_type="stage_1_complete",
                   total_analyzed=len(symbols),
                   passed=len(suitable_stocks),
                   rejected=len(rejected_stocks),
                   passed_symbols=[s['symbol'] for s in suitable_stocks],
                   rejected_symbols=rejected_stocks)

        return suitable_stocks
    
    def get_option_chain_with_analysis(self, symbol: str) -> Dict[str, List[Dict[str, Any]]]:
        """Get options chain with analysis for wheel strategy.

        Results are cached for up to 5 minutes to avoid repeated API calls
        when both find_suitable_puts() and find_suitable_calls() need the
        same chain.

        Args:
            symbol: Underlying stock symbol

        Returns:
            Dictionary with 'puts' and 'calls' lists
        """
        try:
            # Check cache first
            cached = self._options_chain_cache.get(symbol)
            if cached is not None:
                value, ts = cached
                if clock.now() - ts < self._cache_ttl:
                    logger.debug("Using cached options chain",
                                event_category="data",
                                event_type="options_chain_cache_hit",
                                symbol=symbol)
                    return value

            options_chain = self.alpaca.get_options_chain(symbol)
            
            if not options_chain:
                logger.warning("No options chain data",
                              event_category="data",
                              event_type="no_options_chain",
                              symbol=symbol)
                return {'puts': [], 'calls': []}
            
            # Get current stock price
            stock_metrics = self.get_stock_metrics(symbol)
            current_price = stock_metrics.get('current_price', 0)
            
            puts = []
            calls = []
            
            now = clock.now()
            
            for option in options_chain:
                # Calculate days to expiration
                exp_date = option['expiration_date']

                # Skip options with missing expiration date
                if exp_date is None:
                    continue

                if isinstance(exp_date, str):
                    exp_date = datetime.fromisoformat(exp_date.replace('Z', '+00:00'))

                dte = (exp_date - now).days
                
                # Calculate mid price
                if option['bid'] > 0 and option['ask'] > 0:
                    mid_price = (option['bid'] + option['ask']) / 2
                else:
                    mid_price = option['last_price']
                
                # Add calculated fields
                option_data = option.copy()
                option_data.update({
                    'dte': dte,
                    'mid_price': mid_price,
                    'bid_ask_spread': option['ask'] - option['bid'] if option['ask'] > option['bid'] else 0,
                    'moneyness': option['strike_price'] / current_price if current_price > 0 else 0
                })
                
                # Separate puts and calls
                if option['option_type'].upper() == 'PUT':
                    puts.append(option_data)
                else:
                    calls.append(option_data)
            
            result = {'puts': puts, 'calls': calls}
            self._options_chain_cache[symbol] = (result, clock.now())
            return result

        except Exception as e:
            logger.error("Failed to get options chain",
                        event_category="error",
                        event_type="options_chain_error",
                        symbol=symbol,
                        error=str(e))
            return {'puts': [], 'calls': []}
    
    def find_suitable_puts(self, symbol: str) -> List[Dict[str, Any]]:
        """Find put options suitable for selling in wheel strategy.
        
        Args:
            symbol: Underlying stock symbol
            
        Returns:
            List of suitable put options, sorted by attractiveness
        """
        options_data = self.get_option_chain_with_analysis(symbol)
        puts = options_data['puts']

        logger.info("STAGE 7: Options chain criteria - starting put scan",
                   event_category="filtering",
                   event_type="stage_7_start",
                   symbol=symbol,
                   total_puts_in_chain=len(puts),
                   target_dte=self.config.put_target_dte,
                   min_premium=self.config.min_put_premium,
                   delta_range=self.config.put_delta_range)

        suitable_puts = []

        # Track rejection reasons for detailed logging
        rejection_stats = {
            'dte_too_high': 0,
            'premium_too_low': 0,
            'delta_out_of_range': 0,
            'no_liquidity': 0,
            'total_rejected': 0
        }

        # Track min/max values of rejected puts for analysis
        rejected_puts_data = {
            'premiums': [],
            'deltas': [],
            'dtes': []
        }

        for put in puts:
            # First validate option data integrity
            is_valid, validation_reason = self._validate_option_data(put)
            if not is_valid:
                rejection_stats['total_rejected'] += 1
                logger.debug("Put option failed data validation",
                            event_category="filtering",
                            event_type="put_validation_failed",
                            symbol=put.get('symbol', 'unknown'),
                            reason=validation_reason)
                continue

            # Filter by strategy criteria with detailed rejection tracking
            rejection_reason = self._check_put_criteria_detailed(put)

            if rejection_reason:
                rejection_stats['total_rejected'] += 1
                rejection_stats[rejection_reason] += 1

                # Collect data for rejected puts
                rejected_puts_data['premiums'].append(put['mid_price'])
                rejected_puts_data['deltas'].append(abs(put.get('delta', 0)))
                rejected_puts_data['dtes'].append(put['dte'])
                continue

            # Calculate annualized return
            if put['dte'] > 0 and put['mid_price'] > 0:
                annual_return = (put['mid_price'] / put['strike_price']) * (365 / put['dte'])
                put['annual_return'] = annual_return
                put['return_score'] = annual_return * (1 - abs(put.get('delta', 0)))  # Prefer lower delta

                suitable_puts.append(put)

        # Sort by return score (higher is better)
        suitable_puts.sort(key=lambda x: x.get('return_score', 0), reverse=True)

        if suitable_puts:
            best_put = suitable_puts[0]
            logger.info("STAGE 7 COMPLETE: Options chain criteria - puts found",
                       event_category="filtering",
                       event_type="stage_7_complete_found",
                       symbol=symbol,
                       total_puts=len(puts),
                       rejected=rejection_stats['total_rejected'],
                       suitable_count=len(suitable_puts),
                       best_put_strike=best_put['strike_price'],
                       best_put_premium=best_put['mid_price'],
                       best_put_dte=best_put['dte'],
                       best_put_delta=best_put.get('delta', 0),
                       # Detailed rejection breakdown
                       rejected_dte_too_high=rejection_stats['dte_too_high'],
                       rejected_premium_too_low=rejection_stats['premium_too_low'],
                       rejected_delta_out_of_range=rejection_stats['delta_out_of_range'],
                       rejected_no_liquidity=rejection_stats['no_liquidity'])
        else:
            # Calculate stats for rejected puts
            avg_premium = sum(rejected_puts_data['premiums']) / len(rejected_puts_data['premiums']) if rejected_puts_data['premiums'] else 0
            avg_delta = sum(rejected_puts_data['deltas']) / len(rejected_puts_data['deltas']) if rejected_puts_data['deltas'] else 0
            max_premium = max(rejected_puts_data['premiums']) if rejected_puts_data['premiums'] else 0
            min_dte = min(rejected_puts_data['dtes']) if rejected_puts_data['dtes'] else 0
            max_dte = max(rejected_puts_data['dtes']) if rejected_puts_data['dtes'] else 0

            logger.info("STAGE 7 COMPLETE: Options chain criteria - NO suitable puts",
                       event_category="filtering",
                       event_type="stage_7_complete_not_found",
                       symbol=symbol,
                       total_puts=len(puts),
                       rejected=rejection_stats['total_rejected'],
                       reason="no_puts_met_criteria",
                       # Detailed rejection breakdown
                       rejected_dte_too_high=rejection_stats['dte_too_high'],
                       rejected_premium_too_low=rejection_stats['premium_too_low'],
                       rejected_delta_out_of_range=rejection_stats['delta_out_of_range'],
                       rejected_no_liquidity=rejection_stats['no_liquidity'],
                       # Stats on rejected puts
                       rejected_avg_premium=round(avg_premium, 2),
                       rejected_avg_delta=round(avg_delta, 4),
                       rejected_max_premium=round(max_premium, 2),
                       rejected_dte_range=f"{min_dte}-{max_dte}")

        return suitable_puts[:5]  # Return top 5
    
    def find_suitable_calls(self, symbol: str, min_strike_price: float = 0.0,
                            exclude_expiry_on_or_after: Optional[date] = None,
                            include_expiry_on_or_before: Optional[date] = None,
                            criteria_profile: str = 'entry'
                            ) -> List[Dict[str, Any]]:
        """Find call options suitable for selling in wheel strategy.

        Args:
            symbol: Underlying stock symbol
            min_strike_price: Minimum strike price (typically cost basis to avoid guaranteed losses)
            exclude_expiry_on_or_after: FC-013 DD-3 — the earnings **span**
                predicate. When set, a candidate whose ``expiration_date`` falls
                on or after this date is rejected: any call whose expiry covers
                the report carries the full gap (AMZN C262.5, +12.3% gap
                *through* the strike, $2,308 surrendered against $222
                collected). Candidates expiring strictly *before* the event
                proceed untouched — that allowed case is what makes span
                DTE-invariant, and it is why the operator rejected both a
                hardcoded N and a derive-N-from-DTE proxy.

                ``>=`` is inclusive on purpose: assignment on an expiry that
                lands on the report date resolves *after* the report, so
                expiry-day-equals-event-day carries the gap like any other
                spanning contract.

                ``None`` (the default) is today's behaviour, byte-identical —
                the parameter is inert for every caller that does not pass it.

            include_expiry_on_or_before: FC-078 DD-3 — the roll-out **horizon**
                ceiling, symmetric with (and applied alongside) the span floor
                above. A candidate whose ``expiration_date`` falls strictly
                after this date is rejected.

                The bound is per-position (old expiry + ``max_extension_days``,
                an expiry-relative frame), which is why it arrives as a date
                rather than being read from config here: the shared filter
                cannot know which contract is being rolled. ``None`` (the
                default) is inert.

            criteria_profile: which criteria set ``_check_call_criteria_detailed``
                applies. ``'entry'`` (the default) is today's behaviour,
                byte-identical — DTE cap, ``min_call_premium`` floor, the entry
                delta band, liquidity. ``'roll'`` (FC-078 DD-3) drops the DTE
                cap (the expiry bound arrives as ``include_expiry_on_or_before``
                instead), drops the premium floor (the roll's economics are the
                *net* credit, not the standalone premium), and replaces the
                entry delta band with a bare upper rail
                ``rolling_max_replacement_delta`` — the band's *lower* bound is
                the trap that made shallow-ITM rescue illegal. Liquidity is kept.

                The ``'roll'`` profile additionally returns the **full** legal
                candidate list rather than the entry path's top-5-by-return_score
                slice. FC-078 DD-3 selects the executed contract by maximum net
                credit, and ``return_score`` (annualised yield x OTM preference)
                is entry-shaped: truncating by it would drop near-money,
                high-credit replacements before the roller ever ranked them —
                the same failure mode DD-3 rejected first-past-the-post
                ordering for. See the FC-078 PR body, "plan gap 1".

        Returns:
            List of suitable call options, sorted by attractiveness
        """
        span_floor = _coerce_date(exclude_expiry_on_or_after)
        horizon_ceiling = _coerce_date(include_expiry_on_or_before)
        if criteria_profile not in ('entry', 'roll'):
            raise ValueError(
                f"Unknown criteria_profile {criteria_profile!r} (expected "
                "'entry' or 'roll')")
        options_data = self.get_option_chain_with_analysis(symbol)
        calls = options_data['calls']

        logger.info("STAGE 8: Call options criteria - starting call scan",
                   event_category="filtering",
                   event_type="stage_8_start",
                   symbol=symbol,
                   total_calls_in_chain=len(calls),
                   target_dte=self.config.call_target_dte,
                   min_premium=self.config.min_call_premium,
                   delta_range=self.config.call_delta_range,
                   min_strike_price=min_strike_price,
                   criteria_profile=criteria_profile,
                   exclude_expiry_on_or_after=(
                       span_floor.isoformat() if span_floor else None),
                   include_expiry_on_or_before=(
                       horizon_ceiling.isoformat() if horizon_ceiling else None))

        # Warn if cost basis protection is not being used. Pre-FC-029 this
        # warning fired for legitimate put-only scans too (no shares held);
        # the sole remaining caller (`OptionsScanner.scan_for_call_opportunities`
        # — FC-068 deleted `call_seller.evaluate_covered_call_opportunity`)
        # always passes a non-zero floor when shares are held, so this warning
        # now only fires when the floor genuinely failed to resolve from any
        # source — a real risk signal worth investigating.
        if min_strike_price <= 0.0:
            logger.warning("find_suitable_calls called without cost basis protection",
                          event_category="risk",
                          event_type="missing_cost_basis_filter",
                          symbol=symbol)

        suitable_calls = []

        # Track rejection reasons for detailed logging
        rejection_stats = {
            'below_cost_basis': 0,
            'dte_too_high': 0,
            'premium_too_low': 0,
            'delta_out_of_range': 0,
            'no_liquidity': 0,
            # FC-075 Phase 2 (DD-3): inventory-validator chain criteria. Only
            # non-zero when the covered-call profile sets universe.min_open_interest
            # / universe.max_spread_pct; must be pre-initialized here because
            # `rejection_stats[reason] += 1` below KeyErrors on an unknown key.
            'open_interest_too_low': 0,
            'spread_too_wide': 0,
            # FC-013: counted LAST in the chain, so it means "a strike that
            # otherwise qualified was removed because it expires into
            # earnings" — not "some contract in the chain happens to span".
            # OptionsScanner reads exactly that meaning: an empty result with
            # this counter > 0 is a span-emptied symbol (a decision row),
            # while an empty result with it at 0 is ordinary `no_candidates`.
            'expires_into_earnings': 0,
            # FC-078: expiry beyond the roll-out horizon (old expiry + N). Only
            # ever non-zero on the 'roll' profile, where the DTE cap is off and
            # this date bound replaces it.
            'expiry_beyond_horizon': 0,
            # Contracts dropped by `_validate_option_data` before any criterion
            # runs. Previously counted only in `total_rejected`, which made the
            # STAGE 8 buckets sum to less than `rejected=` with no way to tell
            # where the difference went.
            'data_validation_failed': 0,
            'total_rejected': 0
        }

        # Track min/max values of rejected calls for analysis
        rejected_calls_data = {
            'premiums': [],
            'deltas': [],
            'dtes': [],
            'strikes': []
        }

        for call in calls:
            # First validate option data integrity
            is_valid, validation_reason = self._validate_option_data(call)
            if not is_valid:
                rejection_stats['total_rejected'] += 1
                rejection_stats['data_validation_failed'] += 1
                logger.debug("Call option failed data validation",
                            event_category="filtering",
                            event_type="call_validation_failed",
                            symbol=call.get('symbol', 'unknown'),
                            reason=validation_reason)
                continue

            # CRITICAL: Cost basis protection - never sell calls below cost basis
            if min_strike_price > 0 and call['strike_price'] < min_strike_price:
                rejection_stats['total_rejected'] += 1
                rejection_stats['below_cost_basis'] += 1
                rejected_calls_data['strikes'].append(call['strike_price'])
                rejected_calls_data['premiums'].append(call['mid_price'])
                rejected_calls_data['deltas'].append(abs(call.get('delta', 0)))
                rejected_calls_data['dtes'].append(call['dte'])
                continue

            # Filter by strategy criteria with detailed rejection tracking
            rejection_reason = self._check_call_criteria_detailed(
                call, profile=criteria_profile)

            if rejection_reason:
                rejection_stats['total_rejected'] += 1
                rejection_stats[rejection_reason] += 1

                # Collect data for rejected calls
                rejected_calls_data['premiums'].append(call['mid_price'])
                rejected_calls_data['deltas'].append(abs(call.get('delta', 0)))
                rejected_calls_data['dtes'].append(call['dte'])
                rejected_calls_data['strikes'].append(call['strike_price'])
                continue

            # FC-078 DD-3: the roll-out horizon ceiling. Applied before the span
            # predicate so the FC-013 counter keeps its meaning ("how many
            # in-horizon strikes did the event take") rather than being inflated
            # by contracts that were out of reach anyway. Inert on the entry
            # profile, which never passes the bound.
            if horizon_ceiling is not None:
                expiry = _coerce_date(call.get('expiration_date'))
                # Same fail-closed posture as the span floor: an expiry we
                # cannot parse is rejected. "We could not tell how far out this
                # expires" must not resolve to "sell it".
                if expiry is None or expiry > horizon_ceiling:
                    rejection_stats['total_rejected'] += 1
                    rejection_stats['expiry_beyond_horizon'] += 1
                    rejected_calls_data['premiums'].append(call['mid_price'])
                    rejected_calls_data['deltas'].append(abs(call.get('delta', 0)))
                    rejected_calls_data['dtes'].append(call['dte'])
                    rejected_calls_data['strikes'].append(call['strike_price'])
                    continue

            # FC-013 DD-3: the earnings SPAN predicate, applied last so the
            # counter answers "how many QUALIFYING strikes did the event take".
            # A contract that would have failed delta/DTE/premium anyway is
            # attributed to that reason, not to earnings — otherwise a symbol
            # with no tradeable strikes at all would be filed as span-emptied.
            if span_floor is not None:
                expiry = _coerce_date(call.get('expiration_date'))
                # An expiry we cannot parse is rejected too: this is a risk
                # control, and "we could not tell whether it spans" must not
                # resolve to "sell it". Structurally unreachable today —
                # get_option_chain_with_analysis already drops contracts with
                # no expiration_date — which is exactly why it must not be the
                # one path that fails open if that ever changes.
                if expiry is None or expiry >= span_floor:
                    rejection_stats['total_rejected'] += 1
                    rejection_stats['expires_into_earnings'] += 1
                    rejected_calls_data['premiums'].append(call['mid_price'])
                    rejected_calls_data['deltas'].append(abs(call.get('delta', 0)))
                    rejected_calls_data['dtes'].append(call['dte'])
                    rejected_calls_data['strikes'].append(call['strike_price'])
                    logger.debug("Call rejected - expiry spans earnings",
                                 event_category="filtering",
                                 event_type="call_rejected_expires_into_earnings",
                                 symbol=symbol,
                                 option_symbol=call.get('symbol'),
                                 expiration_date=expiry.isoformat() if expiry else None,
                                 next_earnings_date=span_floor.isoformat())
                    continue

            # Calculate annualized return
            if call['dte'] > 0 and call['mid_price'] > 0:
                annual_return = (call['mid_price'] / call['strike_price']) * (365 / call['dte'])
                call['annual_return'] = annual_return
                call['return_score'] = annual_return * (1 - abs(call.get('delta', 0)))  # Prefer lower delta

                suitable_calls.append(call)

        # Sort by return score (higher is better)
        suitable_calls.sort(key=lambda x: x.get('return_score', 0), reverse=True)

        # FC-065 Phase 4: publish the breakdown for the decision record. Same
        # numbers the STAGE 8 log line carries, but reachable by the caller
        # instead of only by log archaeology.
        self.last_call_rejection_stats[symbol] = dict(rejection_stats)

        if suitable_calls:
            best_call = suitable_calls[0]
            logger.info("STAGE 8 COMPLETE: Call options criteria - calls found",
                       event_category="filtering",
                       event_type="stage_8_complete_found",
                       symbol=symbol,
                       total_calls=len(calls),
                       rejected=rejection_stats['total_rejected'],
                       suitable_count=len(suitable_calls),
                       best_call_strike=best_call['strike_price'],
                       best_call_premium=best_call['mid_price'],
                       best_call_dte=best_call['dte'],
                       best_call_delta=best_call.get('delta', 0),
                       # Detailed rejection breakdown
                       rejected_below_cost_basis=rejection_stats['below_cost_basis'],
                       rejected_dte_too_high=rejection_stats['dte_too_high'],
                       rejected_premium_too_low=rejection_stats['premium_too_low'],
                       rejected_delta_out_of_range=rejection_stats['delta_out_of_range'],
                       rejected_no_liquidity=rejection_stats['no_liquidity'],
                       rejected_expires_into_earnings=rejection_stats['expires_into_earnings'],
                       rejected_expiry_beyond_horizon=rejection_stats['expiry_beyond_horizon'],
                       rejected_open_interest_too_low=rejection_stats['open_interest_too_low'],
                       rejected_spread_too_wide=rejection_stats['spread_too_wide'],
                       rejected_data_validation_failed=rejection_stats['data_validation_failed'],
                       criteria_profile=criteria_profile)
        else:
            # Calculate stats for rejected calls
            avg_premium = sum(rejected_calls_data['premiums']) / len(rejected_calls_data['premiums']) if rejected_calls_data['premiums'] else 0
            avg_delta = sum(rejected_calls_data['deltas']) / len(rejected_calls_data['deltas']) if rejected_calls_data['deltas'] else 0
            max_premium = max(rejected_calls_data['premiums']) if rejected_calls_data['premiums'] else 0
            min_dte = min(rejected_calls_data['dtes']) if rejected_calls_data['dtes'] else 0
            max_dte = max(rejected_calls_data['dtes']) if rejected_calls_data['dtes'] else 0

            logger.info("STAGE 8 COMPLETE: Call options criteria - NO suitable calls",
                       event_category="filtering",
                       event_type="stage_8_complete_not_found",
                       symbol=symbol,
                       total_calls=len(calls),
                       rejected=rejection_stats['total_rejected'],
                       reason="no_calls_met_criteria",
                       # Detailed rejection breakdown
                       rejected_below_cost_basis=rejection_stats['below_cost_basis'],
                       rejected_dte_too_high=rejection_stats['dte_too_high'],
                       rejected_premium_too_low=rejection_stats['premium_too_low'],
                       rejected_delta_out_of_range=rejection_stats['delta_out_of_range'],
                       rejected_no_liquidity=rejection_stats['no_liquidity'],
                       rejected_expires_into_earnings=rejection_stats['expires_into_earnings'],
                       rejected_expiry_beyond_horizon=rejection_stats['expiry_beyond_horizon'],
                       rejected_open_interest_too_low=rejection_stats['open_interest_too_low'],
                       rejected_spread_too_wide=rejection_stats['spread_too_wide'],
                       rejected_data_validation_failed=rejection_stats['data_validation_failed'],
                       criteria_profile=criteria_profile,
                       # Stats on rejected calls
                       rejected_avg_premium=round(avg_premium, 2),
                       rejected_avg_delta=round(avg_delta, 4),
                       rejected_max_premium=round(max_premium, 2),
                       rejected_dte_range=f"{min_dte}-{max_dte}")

        # The entry path keeps its top-5 slice, byte-identical. The roll path
        # gets the full legal set: FC-078 DD-3 ranks by net credit, and slicing
        # by the entry-shaped ``return_score`` first would hide exactly the
        # near-money, high-credit replacements a defensive roll wants.
        if criteria_profile == 'roll':
            return suitable_calls
        return suitable_calls[:5]  # Return top 5

    def _validate_option_data(self, option: Dict[str, Any]) -> tuple:
        """Validate option data before processing.

        Args:
            option: Option data dictionary

        Returns:
            Tuple of (is_valid: bool, reason: str)
        """
        # Check required quote fields exist
        bid = option.get('bid')
        ask = option.get('ask')

        if bid is None or ask is None:
            return False, "missing_quotes"

        # Validate quote values are sensible
        if not isinstance(bid, (int, float)) or not isinstance(ask, (int, float)):
            return False, "invalid_quote_type"

        if bid < 0 or ask < 0:
            return False, "negative_quotes"

        if bid > ask:
            return False, "inverted_spread"

        # Check delta exists (critical for options trading)
        delta = option.get('delta')
        if delta is None:
            return False, "missing_delta"

        # Check DTE is valid
        dte = option.get('dte')
        if dte is None or dte < 0:
            return False, "invalid_dte"

        # Check volume/open_interest exist (for liquidity assessment)
        if option.get('volume') is None or option.get('open_interest') is None:
            return False, "missing_liquidity_data"

        return True, "valid"

    def _check_put_criteria_detailed(self, put_option: Dict[str, Any]) -> Optional[str]:
        """Check if put option meets criteria and return specific rejection reason.

        Args:
            put_option: Put option data

        Returns:
            Rejection reason key if rejected, None if passes all criteria
        """
        # DTE criteria - check first as it's most common
        max_dte = self.config.put_target_dte
        if put_option['dte'] > max_dte:
            return 'dte_too_high'

        # Premium criteria
        if put_option['mid_price'] < self.config.min_put_premium:
            return 'premium_too_low'

        # Delta criteria
        delta = abs(put_option.get('delta', 0))
        delta_range = self.config.put_delta_range
        if not (delta_range[0] <= delta <= delta_range[1]):
            return 'delta_out_of_range'

        # Liquidity criteria (basic)
        if put_option['volume'] == 0 and put_option['open_interest'] < 10:
            return 'no_liquidity'

        return None  # Passes all criteria

    def _meets_put_criteria(self, put_option: Dict[str, Any]) -> bool:
        """Check if put option meets wheel strategy criteria.

        Args:
            put_option: Put option data

        Returns:
            True if option meets criteria
        """
        # Use detailed checker
        return self._check_put_criteria_detailed(put_option) is None

    def _check_call_criteria_detailed(self, call_option: Dict[str, Any],
                                      profile: str = 'entry') -> Optional[str]:
        """Check if call option meets criteria and return specific rejection reason.

        Args:
            call_option: Call option data
            profile: ``'entry'`` (default, byte-identical to pre-FC-078
                behaviour) or ``'roll'`` (FC-078 DD-3 — no DTE cap, no premium
                floor, delta upper rail only, liquidity kept).

        Returns:
            Rejection reason key if rejected, None if passes all criteria
        """
        if profile == 'roll':
            # No DTE check: the roll horizon is expiry-relative and per-position,
            # so it is applied in find_suitable_calls as a date bound, not here.
            #
            # No premium floor: what a roll must clear is the NET credit against
            # the contract being closed, not a standalone premium. min_call_premium
            # is inert on ITM replacements (they carry large premiums) where it
            # isn't simply the wrong question.
            #
            # Delta: upper rail only. The entry band's lower bound forces far-OTM
            # replacements exactly when a defensive roll needs near-money strikes —
            # the documented trap (FC-066 / FC-078 DD-3). The rail blocks
            # replace-ITM-with-deep-ITM churn, which pins shares under a
            # near-certain-assignment cap for another two weeks for pennies.
            delta = abs(call_option.get('delta', 0))
            if delta > self.config.rolling_max_replacement_delta:
                return 'delta_out_of_range'

            # Liquidity criteria: kept, identically.
            if call_option['volume'] == 0 and call_option['open_interest'] < 10:
                return 'no_liquidity'

            return None

        # DTE criteria - check first as it's most common
        max_dte = self.config.call_target_dte
        if call_option['dte'] > max_dte:
            return 'dte_too_high'

        # Premium criteria
        if call_option['mid_price'] < self.config.min_call_premium:
            return 'premium_too_low'

        # Delta criteria
        delta = abs(call_option.get('delta', 0))
        delta_range = self.config.call_delta_range
        if not (delta_range[0] <= delta <= delta_range[1]):
            return 'delta_out_of_range'

        # Liquidity criteria (basic)
        if call_option['volume'] == 0 and call_option['open_interest'] < 10:
            return 'no_liquidity'

        # FC-075 Phase 2 (DD-3): inventory-validator chain criteria. Both inert
        # unless the covered-call profile sets the thresholds (wheel: None →
        # skipped → byte-identical). Fail closed — a threshold set against a
        # missing/None field (or a non-positive mid) rejects the strike rather
        # than passing it unverified.
        min_oi = self.config.min_open_interest
        if min_oi is not None:
            oi = call_option.get('open_interest')
            if oi is None or oi < min_oi:
                return 'open_interest_too_low'

        max_spread = self.config.max_spread_pct
        if max_spread is not None:
            bid = call_option.get('bid')
            ask = call_option.get('ask')
            mid = call_option.get('mid_price')
            if bid is None or ask is None or mid is None or mid <= 0:
                return 'spread_too_wide'
            if (ask - bid) / mid > max_spread:
                return 'spread_too_wide'

        return None  # Passes all criteria

    def _meets_call_criteria(self, call_option: Dict[str, Any]) -> bool:
        """Check if call option meets wheel strategy criteria.

        Args:
            call_option: Call option data

        Returns:
            True if option meets criteria
        """
        # Use detailed checker
        return self._check_call_criteria_detailed(call_option) is None