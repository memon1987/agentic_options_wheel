"""Core options wheel strategy engine."""

from typing import Dict, Any, Optional
from datetime import datetime, timedelta
import structlog

from ..utils import clock
from ..api.alpaca_client import AlpacaClient
from ..api.market_data import MarketDataManager
from ..utils.config import Config
from ..utils.logging_events import log_system_event, log_trade_event, log_error_event
from ..utils.positions import get_stock_positions
from ..utils.option_symbols import parse_option_symbol, strict_option_type
from .call_roller import CallRoller
from .wheel_state_manager import WheelStateManager
from ..risk.risk_manager import RiskManager
from ..api.earnings_calendar import EarningsCalendarService

logger = structlog.get_logger(__name__)

# FC-078 §4 — roll-cycle time budget, in seconds.
#
# The daily scheduler job is created with --attempt-deadline 1800s. The budget
# is that minus headroom, and no position is *started* unless the full
# per-position worst case remains: BTC 120 s poll + cancel/verify + up to four
# STO rungs x 120 s. Realistic load is 0-1 executable roll per day; this guard
# exists for the pathological day, where the alternative is a request killed
# between a filled BTC and an unplaced STO.
_CYCLE_BUDGET_SECONDS = 1500
_PER_POSITION_BUDGET_SECONDS = 600


class WheelEngine:
    """Position housekeeping and the daily roll cycle.

    FC-068 deleted the orchestration half of this class. ``run_strategy_cycle``
    and everything beneath it (``_find_new_opportunities``,
    ``_manage_existing_positions``, the gap/wheel-state/per-cycle-cap stages)
    had not been called by production since 2025-10-03 — three days before the
    live account's first fill — and the backtest was the only surviving caller.
    Candidate generation lives on the production path only:

        ``/scan`` → :class:`~src.data.options_scanner.OptionsScanner`
        → ``/run`` → :class:`~src.strategy.execution_engine.ExecutionEngine`
        → ``PutSeller.execute_put_sale`` / ``CallSeller.execute_call_sale``

    What survives here is what production actually calls: ``reconcile_positions``
    (pre-trade housekeeping on ``/run``) and ``run_rolling_cycle`` (the Friday
    ``/roll`` scheduler).
    """

    def __init__(self, config: Config, alpaca_client: Optional[AlpacaClient] = None,
                 wheel_state: Optional[WheelStateManager] = None,
                 allow_bigquery_cost_basis: bool = True,
                 earnings_calendar: Optional[Any] = None):
        """Initialize the wheel strategy engine.

        Args:
            config: Configuration instance
            alpaca_client: Broker/data client. Defaults to the real AlpacaClient.
                A backtest injects an adapter here; because every downstream
                component already takes the client by constructor injection,
                this single seam redirects the whole graph.
            wheel_state: Reconciliation's in-request position bookkeeping.
                Defaults to a fresh, empty one. FC-069 item 8 (stage 2) removed
                the GCS persistence this used to be pointed at — it had never
                been enabled (FC-039), so "defaults to the configured bucket"
                always meant "in-memory", and now says so. The parameter stays
                because the backtest injects its own instance.
            allow_bigquery_cost_basis: forwarded to CallRoller (FC-065
                Phase 2), which resolves its strike floor through the shared
                resolver. A backtest passes False so it cannot query production
                trade history mid-replay — run_rolling_cycle() runs inside the
                replay too. (Pre-FC-068 this was also forwarded to a CallSeller
                the engine constructed; that seller and its resolver are gone.)
            earnings_calendar: earnings service used by the rolling cycle.
                Defaults to constructing the live Finnhub-backed service. A
                backtest injects a point-in-time calendar, because Finnhub can
                only answer "when is the *next* earnings date" — meaningless
                when replaying 2024.
        """
        self.config = config
        self.alpaca = alpaca_client if alpaca_client is not None else AlpacaClient(config)
        self.market_data = MarketDataManager(self.alpaca, config)
        self.wheel_state = wheel_state if wheel_state is not None else WheelStateManager()
        # Retained for the rolling cycle, which builds its own CallRoller (and
        # therefore its own CostBasisResolver) per invocation — FC-065 Phase 2.
        self._allow_bigquery_cost_basis = allow_bigquery_cost_basis

        self._injected_earnings_calendar = earnings_calendar

        logger.info("Wheel engine initialized with state management",
                   event_category="system", event_type="engine_initialized")

    def _extract_underlying_from_option_symbol(self, option_symbol: str) -> Optional[str]:
        """Extract underlying stock symbol from option symbol.

        Delegates to the shared parse_option_symbol() utility.

        Args:
            option_symbol: Full option symbol

        Returns:
            Underlying stock symbol or None
        """
        parsed = parse_option_symbol(option_symbol)
        underlying = parsed.get('underlying')
        return underlying if underlying else None

    def _count_option_legs(self, option_positions: list) -> Dict[str, Dict[str, int]]:
        """Tally Alpaca option positions per underlying, by leg.

        Returns ``{underlying: {'puts': n, 'calls': n, 'unclassified': n}}``,
        counted in contracts (short positions carry a negative qty; the sign is
        dropped — this is an inventory count, not a signed exposure).

        FC-079. This used to classify with ``'P' in option_symbol`` / ``elif
        'C' in option_symbol``, testing the whole contract string rather than
        the type character. Every underlying whose root carries a P or a C was
        misread — AAPL, SPY and PFE all contain a ``'P'``, so the put branch
        fired first and every one of their *calls* was counted as a put. These
        counts feed the position-diff assignment fallback in
        ``reconcile_positions``, so on a third of the universe a call
        assignment could never be detected on that path.

        Classification is strict, and it never guesses: a symbol that is not an
        exact OCC contract (an adjusted root after a split, e.g.
        ``AAPL1260918C00230000``) is counted under ``unclassified`` and warned
        once per symbol per run — not dropped, and not attributed to a leg.
        Guessing a leg here would feed a fabricated assignment into
        ``handle_put_assignment`` / ``handle_call_assignment``; dropping it
        would make the position vanish from the diff, which reads as an
        assignment that did not happen.

        Args:
            option_positions: Alpaca option positions (``asset_class`` already
                filtered to ``us_option``)

        Returns:
            Per-underlying leg counts
        """
        counts: Dict[str, Dict[str, int]] = {}
        warned = set()

        for pos in option_positions:
            option_symbol = pos['symbol']
            qty = int(float(pos['qty']))
            underlying = self._extract_underlying_from_option_symbol(option_symbol)
            if not underlying:
                continue
            if underlying not in counts:
                counts[underlying] = {'puts': 0, 'calls': 0, 'unclassified': 0}
            # Short positions have negative qty
            contracts = abs(qty)
            option_type = strict_option_type(option_symbol)
            if option_type == 'put':
                counts[underlying]['puts'] += contracts
            elif option_type == 'call':
                counts[underlying]['calls'] += contracts
            else:
                counts[underlying]['unclassified'] += contracts
                if option_symbol not in warned:
                    warned.add(option_symbol)
                    logger.warning(
                        "Option position is not a strict OCC contract - not counted as a leg",
                        event_category="reconciliation",
                        event_type="reconcile_unclassifiable_option",
                        symbol=option_symbol,
                        underlying=underlying,
                        contracts=contracts)

        return counts

    def reconcile_positions(self) -> Dict[str, Any]:
        """Reconcile tracked wheel state against actual Alpaca positions.

        Alpaca is the source of truth. This method fetches real positions,
        compares them against the wheel state manager's tracked state, logs
        any discrepancies as warnings, and updates the state manager to
        match reality.

        Returns:
            Summary of reconciliation actions taken
        """
        try:
            logger.info("Starting position reconciliation",
                       event_category="system",
                       event_type="reconciliation_started")

            stats = {
                'stock_positions_alpaca': 0,
                'option_positions_alpaca': 0,
                'symbols_in_state': len(self.wheel_state.symbol_states),
                'discrepancies_found': 0,
                'state_updates': 0,
                'orphaned_state_entries_cleared': 0,
            }

            # --- Primary: Check Alpaca Activities API for assignments/expirations ---
            try:
                cutoff_date = (clock.now() - timedelta(days=7)).strftime('%Y-%m-%d')
                activities = self.alpaca.get_account_activities(
                    'OPASN,OPEXP', after=cutoff_date
                )

                # Track which activities we've already processed (by id) to avoid
                # double-counting on repeated reconciliation runs.
                if not hasattr(self, '_processed_activity_ids'):
                    self._processed_activity_ids = set()

                for activity in activities:
                    activity_id = activity.get('id', '')
                    if activity_id in self._processed_activity_ids:
                        continue

                    activity_type = activity.get('activity_type', '')
                    act_symbol = activity.get('symbol', '')
                    act_qty = abs(int(float(activity.get('qty', 0))))
                    act_date_str = activity.get('date', '') or activity.get('transaction_time', '')
                    net_amount = float(activity.get('net_amount', 0))

                    # Parse the option symbol to extract underlying and type
                    try:
                        parsed = parse_option_symbol(act_symbol)
                        underlying = parsed.get('underlying', '')
                        option_type = parsed.get('option_type', 'unknown')
                        strike_price = parsed.get('strike_price', 0.0)
                    except Exception:
                        underlying = act_symbol
                        option_type = 'unknown'
                        strike_price = 0.0

                    # Parse the activity date
                    try:
                        if act_date_str:
                            # Handle both YYYY-MM-DD and ISO datetime formats
                            act_date = datetime.fromisoformat(act_date_str.replace('Z', '+00:00'))
                        else:
                            act_date = clock.now()
                    except (ValueError, TypeError):
                        act_date = clock.now()

                    if activity_type == 'OPASN' and underlying:
                        # Option assignment: shares = contracts * 100
                        assigned_shares = act_qty * 100 if act_qty < 100 else act_qty

                        stats.setdefault('activities_assignments_detected', 0)
                        stats['activities_assignments_detected'] += 1

                        if option_type == 'put':
                            # Put assignment: we get assigned shares at strike price
                            per_share_cost = strike_price if strike_price > 0 else abs(net_amount) / max(assigned_shares, 1)
                            try:
                                self.wheel_state.handle_put_assignment(
                                    symbol=underlying,
                                    shares=assigned_shares,
                                    cost_basis=per_share_cost,
                                    assignment_date=act_date,
                                )
                                log_trade_event(
                                    logger,
                                    event_type="put_assignment_from_activity",
                                    symbol=underlying,
                                    strategy="put_assignment",
                                    success=True,
                                    underlying=underlying,
                                    option_type="PUT",
                                    shares=assigned_shares,
                                    cost_basis=per_share_cost,
                                    activity_id=activity_id,
                                )
                            except Exception as e:
                                logger.warning("Failed to process put assignment activity",
                                              event_category="error",
                                              event_type="activity_put_assignment_failed",
                                              symbol=underlying, activity_id=activity_id,
                                              error=str(e))

                        elif option_type == 'call':
                            # Call assignment: shares are called away at strike price
                            try:
                                self.wheel_state.handle_call_assignment(
                                    symbol=underlying,
                                    shares=assigned_shares,
                                    strike_price=strike_price,
                                    assignment_date=act_date,
                                )
                                log_trade_event(
                                    logger,
                                    event_type="call_assignment_from_activity",
                                    symbol=underlying,
                                    strategy="call_assignment",
                                    success=True,
                                    underlying=underlying,
                                    option_type="CALL",
                                    shares=assigned_shares,
                                    strike_price=strike_price,
                                    activity_id=activity_id,
                                )
                            except Exception as e:
                                logger.warning("Failed to process call assignment activity",
                                              event_category="error",
                                              event_type="activity_call_assignment_failed",
                                              symbol=underlying, activity_id=activity_id,
                                              error=str(e))

                    elif activity_type == 'OPEXP' and underlying:
                        # Option expiration (worthless or exercised)
                        stats.setdefault('activities_expirations_detected', 0)
                        stats['activities_expirations_detected'] += 1
                        logger.info("Option expiration detected via Activities API",
                                   event_category="trade",
                                   event_type="option_expiration_from_activity",
                                   symbol=act_symbol,
                                   underlying=underlying,
                                   option_type=option_type,
                                   qty=act_qty,
                                   activity_id=activity_id)

                    # Mark activity as processed
                    self._processed_activity_ids.add(activity_id)

                if activities:
                    logger.info("Activities API assignment detection completed",
                               event_category="system",
                               event_type="activities_detection_completed",
                               total_activities=len(activities),
                               new_processed=len(activities) - len([
                                   a for a in activities
                                   if a.get('id', '') in self._processed_activity_ids
                               ]))

            except Exception as e:
                logger.warning("Activities API unavailable, falling back to position diff",
                              event_category="system",
                              event_type="activities_api_fallback",
                              error=str(e))

            # --- Fetch actual positions from Alpaca ---
            all_positions = self.alpaca.get_positions()
            # Intentionally not using get_stock_positions() here — reconciliation
            # needs all stock positions including qty=0 to detect closed positions
            stock_positions = [p for p in all_positions if p.get('asset_class') == 'us_equity']
            option_positions = [p for p in all_positions if p.get('asset_class') == 'us_option']

            stats['stock_positions_alpaca'] = len(stock_positions)
            stats['option_positions_alpaca'] = len(option_positions)

            # Build maps of what Alpaca actually holds
            alpaca_stock_shares: Dict[str, int] = {}
            for pos in stock_positions:
                symbol = pos['symbol']
                shares = int(float(pos['qty']))
                if shares > 0:
                    alpaca_stock_shares[symbol] = shares

            alpaca_option_counts = self._count_option_legs(option_positions)

            # --- Compare Alpaca state vs wheel state and reconcile ---

            # 1. Check each symbol tracked in wheel state
            all_tracked_symbols = set(self.wheel_state.symbol_states.keys())
            all_alpaca_symbols = set(alpaca_stock_shares.keys()) | set(alpaca_option_counts.keys())

            for symbol in all_tracked_symbols:
                state_summary = self.wheel_state.get_position_summary(symbol)
                tracked_shares = state_summary['stock_shares']
                tracked_puts = state_summary['active_puts']
                tracked_calls = state_summary['active_calls']

                actual_shares = alpaca_stock_shares.get(symbol, 0)
                actual_opts = alpaca_option_counts.get(
                    symbol, {'puts': 0, 'calls': 0, 'unclassified': 0})
                actual_puts = actual_opts['puts']
                actual_calls = actual_opts['calls']

                # --- Assignment Detection & Wheel State Transition ---
                # Put assignment: puts decreased AND shares increased
                # (broker exercised the put, assigned us stock)
                if tracked_puts > actual_puts and actual_shares > tracked_shares:
                    assigned_contracts = tracked_puts - actual_puts
                    new_shares = actual_shares - tracked_shares
                    stats.setdefault('assignments_detected', 0)
                    stats['assignments_detected'] += assigned_contracts

                    log_trade_event(
                        logger,
                        event_type="put_assignment_detected",
                        symbol=symbol,
                        strategy="put_assignment",
                        success=True,
                        underlying=symbol,
                        option_type="PUT",
                        contracts=assigned_contracts,
                        new_shares=new_shares,
                        previous_puts=tracked_puts,
                        current_puts=actual_puts,
                        previous_shares=tracked_shares,
                        current_shares=actual_shares,
                    )

                    # Trigger wheel state transition: SELLING_PUTS -> HOLDING_STOCK
                    try:
                        # Compute per-share cost basis from Alpaca's total cost_basis
                        # Alpaca returns total cost_basis (price * qty), but
                        # handle_put_assignment expects per-share cost.
                        total_cost_basis = 0.0
                        shares_in_position = 1
                        for pos in stock_positions:
                            if pos.get('symbol') == symbol:
                                total_cost_basis = float(pos.get('cost_basis', 0))
                                shares_in_position = int(float(pos.get('qty', 1)))
                                break
                        per_share_cost = total_cost_basis / max(shares_in_position, 1)
                        self.wheel_state.handle_put_assignment(
                            symbol=symbol,
                            shares=new_shares,
                            cost_basis=per_share_cost,
                            assignment_date=clock.now(),
                        )
                    except Exception as e:
                        logger.warning("Failed to update wheel state for put assignment",
                                      event_category="error",
                                      event_type="put_assignment_state_update_failed",
                                      symbol=symbol, error=str(e))

                # Call assignment: calls decreased AND shares decreased
                # (broker exercised the call, shares called away)
                if tracked_calls > actual_calls and actual_shares < tracked_shares:
                    assigned_contracts = tracked_calls - actual_calls
                    shares_called = tracked_shares - actual_shares
                    stats.setdefault('call_assignments_detected', 0)
                    stats['call_assignments_detected'] += assigned_contracts

                    log_trade_event(
                        logger,
                        event_type="call_assignment_detected",
                        symbol=symbol,
                        strategy="call_assignment",
                        success=True,
                        underlying=symbol,
                        option_type="CALL",
                        contracts=assigned_contracts,
                        shares_called=shares_called,
                        previous_calls=tracked_calls,
                        current_calls=actual_calls,
                        previous_shares=tracked_shares,
                        current_shares=actual_shares,
                    )

                    # Trigger wheel state transition: SELLING_CALLS -> cycle complete
                    # This fires wheel_cycle_complete if all shares are called away
                    try:
                        # Estimate strike from option positions (approximate).
                        # FC-079: ``'C' in opt_sym`` matched the first option
                        # whose *symbol text* contained a C — for PFE that is
                        # every PFE contract, puts included — and the strike
                        # then came off a blind ``[-8:]`` slice. Classify with
                        # strict_option_type and read the strike from the
                        # parser. An unclassifiable symbol is skipped, which
                        # falls through to the current-price fallback below
                        # rather than inventing a strike.
                        strike_price = 0.0
                        for pos in option_positions:
                            opt_sym = pos.get('symbol', '')
                            if strict_option_type(opt_sym) != 'call':
                                continue
                            underlying_of_opt = self._extract_underlying_from_option_symbol(opt_sym)
                            if underlying_of_opt == symbol:
                                try:
                                    strike_price = float(
                                        parse_option_symbol(opt_sym).get('strike_price') or 0.0)
                                except (TypeError, ValueError):
                                    pass
                                break
                        if strike_price == 0.0:
                            # Fallback: use current stock price as estimate
                            for pos in stock_positions:
                                if pos.get('symbol') == symbol:
                                    strike_price = float(pos.get('current_price', 0))
                                    break

                        self.wheel_state.handle_call_assignment(
                            symbol=symbol,
                            shares=shares_called,
                            strike_price=strike_price,
                            assignment_date=clock.now(),
                        )
                    except Exception as e:
                        logger.warning("Failed to update wheel state for call assignment",
                                      event_category="error",
                                      event_type="call_assignment_state_update_failed",
                                      symbol=symbol, error=str(e))

                # Check for stock share discrepancy
                if tracked_shares != actual_shares:
                    stats['discrepancies_found'] += 1
                    logger.warning("Stock share discrepancy detected",
                                  event_category="reconciliation",
                                  event_type="stock_share_mismatch",
                                  symbol=symbol,
                                  tracked_shares=tracked_shares,
                                  actual_shares=actual_shares)
                    log_system_event(
                        logger,
                        event_type="reconciliation_stock_mismatch",
                        status="corrected",
                        symbol=symbol,
                        tracked_shares=tracked_shares,
                        actual_shares=actual_shares)
                    # Update state to match Alpaca
                    self.wheel_state.symbol_states[symbol]['stock_shares'] = actual_shares
                    stats['state_updates'] += 1

                # Check for put count discrepancy
                if tracked_puts != actual_puts:
                    stats['discrepancies_found'] += 1
                    logger.warning("Active puts discrepancy detected",
                                  event_category="reconciliation",
                                  event_type="active_puts_mismatch",
                                  symbol=symbol,
                                  tracked_puts=tracked_puts,
                                  actual_puts=actual_puts)
                    log_system_event(
                        logger,
                        event_type="reconciliation_puts_mismatch",
                        status="corrected",
                        symbol=symbol,
                        tracked_puts=tracked_puts,
                        actual_puts=actual_puts)
                    self.wheel_state.symbol_states[symbol]['active_puts'] = actual_puts
                    stats['state_updates'] += 1

                # Check for call count discrepancy
                if tracked_calls != actual_calls:
                    stats['discrepancies_found'] += 1
                    logger.warning("Active calls discrepancy detected",
                                  event_category="reconciliation",
                                  event_type="active_calls_mismatch",
                                  symbol=symbol,
                                  tracked_calls=tracked_calls,
                                  actual_calls=actual_calls)
                    log_system_event(
                        logger,
                        event_type="reconciliation_calls_mismatch",
                        status="corrected",
                        symbol=symbol,
                        tracked_calls=tracked_calls,
                        actual_calls=actual_calls)
                    self.wheel_state.symbol_states[symbol]['active_calls'] = actual_calls
                    stats['state_updates'] += 1

            # 2. Check for positions in Alpaca not tracked in wheel state
            for symbol in all_alpaca_symbols - all_tracked_symbols:
                stats['discrepancies_found'] += 1
                actual_shares = alpaca_stock_shares.get(symbol, 0)
                actual_opts = alpaca_option_counts.get(
                    symbol, {'puts': 0, 'calls': 0, 'unclassified': 0})

                logger.warning("Position exists in Alpaca but not in wheel state — adding",
                              event_category="reconciliation",
                              event_type="untracked_position_found",
                              symbol=symbol,
                              shares=actual_shares,
                              puts=actual_opts['puts'],
                              calls=actual_opts['calls'])
                log_system_event(
                    logger,
                    event_type="reconciliation_untracked_position",
                    status="added",
                    symbol=symbol,
                    shares=actual_shares,
                    puts=actual_opts['puts'],
                    calls=actual_opts['calls'])

                self.wheel_state.symbol_states[symbol] = {
                    'stock_shares': actual_shares,
                    'stock_cost_basis': 0.0,
                    'acquisition_date': None,
                    'active_puts': actual_opts['puts'],
                    'active_calls': actual_opts['calls'],
                    'wheel_cycle_start': None,
                }
                stats['state_updates'] += 1

            # 3. Clear state entries that have no Alpaca position at all
            for symbol in all_tracked_symbols - all_alpaca_symbols:
                state_summary = self.wheel_state.get_position_summary(symbol)
                has_anything = (state_summary['stock_shares'] > 0
                                or state_summary['active_puts'] > 0
                                or state_summary['active_calls'] > 0)
                if has_anything:
                    stats['discrepancies_found'] += 1
                    logger.warning("Position tracked in wheel state but absent from Alpaca — clearing",
                                  event_category="reconciliation",
                                  event_type="stale_state_entry",
                                  symbol=symbol,
                                  tracked_shares=state_summary['stock_shares'],
                                  tracked_puts=state_summary['active_puts'],
                                  tracked_calls=state_summary['active_calls'])
                    log_system_event(
                        logger,
                        event_type="reconciliation_stale_state_cleared",
                        status="cleared",
                        symbol=symbol)
                    self.wheel_state.symbol_states[symbol]['stock_shares'] = 0
                    self.wheel_state.symbol_states[symbol]['active_puts'] = 0
                    self.wheel_state.symbol_states[symbol]['active_calls'] = 0
                    stats['orphaned_state_entries_cleared'] += 1
                    stats['state_updates'] += 1

            logger.info("Position reconciliation completed",
                       event_category="system",
                       event_type="reconciliation_completed",
                       **stats)

            # No persistence step: the bookkeeping is per-request by design and
            # dies with the request (FC-069 item 8 stage 2 / FC-039). The
            # durable record of this cycle is the events logged above.
            return stats

        except Exception as e:
            logger.error("Position reconciliation failed",
                        event_category="error",
                        event_type="reconciliation_error",
                        error=str(e))
            return {'error': str(e)}

    def _open_order_symbols(self):
        """Symbols with a live open order, and whether we could tell.

        Returns ``(symbols, ok)``. ``ok=False`` means the fetch failed and the
        caller must fail CLOSED — an unknown open-order picture cannot protect
        against the double buy-to-close the guard exists for.

        **FC-043 gotcha, load-bearing:** ``status`` is not a value filter over
        Alpaca's default page. ``'open'`` is a *query token*, never an
        ``OrderStatus`` value, and Alpaca's REST default is open/limit-50.
        ``AlpacaClient.get_orders`` routes the token to ``QueryOrderStatus.OPEN``
        and raises the page size — passing ``'open'`` here is correct only
        because of that routing. Filtering on ``status.value == 'open'``, which
        is what FC-043 found in production, matched nothing at all.
        """
        try:
            orders = self.alpaca.get_orders(status='open') or []
            # Inside the try on purpose: a malformed response is the same
            # failure as a raised one — we cannot see the open-order picture —
            # and it must fail closed rather than take down the whole cycle.
            return {o.get('symbol', '') for o in orders if o.get('symbol')}, True
        except Exception as exc:
            logger.error("Could not fetch open orders for the roll guard",
                         event_category="error",
                         event_type="roll_open_orders_fetch_failed",
                         error=str(exc))
            return set(), False

    def run_rolling_cycle(self) -> Dict[str, Any]:
        """Run the daily credit-only call rolling cycle (FC-006, revived by FC-078).

        Evaluates every short call position for rolling eligibility, then
        executes qualifying rolls via CallRoller. Runs every trading day at
        15:30 ET — after the last ``/monitor`` (profit-taking churn settled) and
        after the last ``/run`` execute (a freshly sold call is OTM by
        construction and simply skips ``not_itm_enough``).

        Returns:
            Summary dict with rolls_evaluated, rolls_executed, rolls_skipped.
        """
        start_time = clock.now()

        if not self.config.rolling_enabled:
            return {'skipped': 'rolling_disabled'}

        log_system_event(logger, event_type="roll_cycle_started", status="starting")

        # Initialize rolling components
        risk_manager = RiskManager(self.config)
        earnings_calendar = self._injected_earnings_calendar
        if earnings_calendar is None and self.config.earnings_enabled:
            earnings_calendar = EarningsCalendarService(self.config)

        roller = CallRoller(
            self.alpaca, self.market_data, self.config,
            risk_manager, earnings_calendar,
            allow_bigquery_cost_basis=self._allow_bigquery_cost_basis)

        # Get all positions
        positions = self.alpaca.get_positions()
        stock_positions = get_stock_positions(positions)
        option_positions = [p for p in positions
                           if p.get('asset_class') == 'us_option']

        # Build stock lookup by symbol
        stock_by_symbol = {}
        for sp in stock_positions:
            sym = sp.get('symbol', '')
            stock_by_symbol[sym] = sp

        # Filter to short call positions
        short_calls = []
        uncovered_calls = []
        for op in option_positions:
            qty = float(op.get('qty', 0))
            if qty >= 0:
                continue  # not short
            parsed = parse_option_symbol(op.get('symbol', ''))
            if parsed.get('option_type') != 'call':
                continue
            underlying = parsed.get('underlying', '')
            if underlying in stock_by_symbol:
                short_calls.append((op, stock_by_symbol[underlying]))
            else:
                # A short call with NO covering shares. The roller cannot roll
                # it (there is no cost-basis floor to resolve), but silently
                # dropping it means the one position shape nobody wants — a
                # genuinely naked short call — is the one the roll cycle never
                # mentions. Emit a terminal so it shows up in the same place
                # every other roll decision does (FC-078 review, trader L-1).
                uncovered_calls.append((op, underlying))

        results = {
            'rolls_evaluated': 0,
            'rolls_executed': 0,
            'rolls_skipped': 0,
            'roll_details': [],
        }

        # FC-078 DD-4: the open-order guard. /monitor places fire-and-forget DAY
        # buy-to-close limits with no cancel, so at 15:30 the roller can see a
        # short call with a live BTC still working against it — rolling it could
        # fill both buys, leaving an unintended LONG call plus a sold
        # replacement. One API call per cycle, not per position.
        open_order_symbols, open_orders_ok = self._open_order_symbols()

        for call_pos, underlying in uncovered_calls:
            results['rolls_evaluated'] += 1
            results['rolls_skipped'] += 1
            roller.log_terminal_skip(
                call_pos.get('symbol', ''), underlying, "no_covering_shares",
                contracts=abs(int(float(call_pos.get('qty', 0)))))

        for call_pos, stock_pos in short_calls:
            results['rolls_evaluated'] += 1
            option_symbol = call_pos.get('symbol', '')
            underlying = self._extract_underlying_from_option_symbol(option_symbol) or ''

            if not open_orders_ok:
                # Fail closed: without the open-order picture the guard cannot
                # protect, and the hazard it guards is a double buy-to-close.
                roller.log_terminal_skip(
                    option_symbol, underlying, "open_orders_unavailable")
                results['rolls_skipped'] += 1
                continue

            # FC-078 §4: cycle time budget. Worst case per position is ~600 s
            # (BTC 120 s poll + cancel/verify + up to 4 STO rungs x 120 s), and
            # a kill BETWEEN the BTC fill and the STO placement is the worst
            # seam there is — shares uncovered, no event, no alert. So a
            # position is never *started* without the full worst case left.
            elapsed = (clock.now() - start_time).total_seconds()
            if _CYCLE_BUDGET_SECONDS - elapsed < _PER_POSITION_BUDGET_SECONDS:
                roller.log_terminal_skip(
                    option_symbol, underlying, "cycle_budget_exhausted",
                    elapsed_seconds=round(elapsed, 1),
                    cycle_budget_seconds=_CYCLE_BUDGET_SECONDS,
                    per_position_budget_seconds=_PER_POSITION_BUDGET_SECONDS)
                results['rolls_skipped'] += 1
                continue

            # Per-position isolation: one bad symbol must not kill the cycle.
            # The except still emits a terminal event, because "exactly one
            # terminal event per evaluated position" has to hold on the paths
            # nobody planned for too.
            try:
                opportunity = roller.evaluate_roll_opportunity(
                    call_pos, stock_pos, open_order_symbols)
            except Exception as exc:
                logger.error("Roll evaluation raised",
                             event_category="error",
                             event_type="roll_position_error",
                             symbol=option_symbol, error=str(exc), exc_info=True)
                roller.log_terminal_skip(
                    option_symbol, underlying, "evaluation_error", error=str(exc))
                results['rolls_skipped'] += 1
                continue

            if not opportunity:
                results['rolls_skipped'] += 1
                continue

            try:
                roll_result = roller.execute_roll(opportunity)
            except Exception as exc:
                # Deliberately NOT a skip event. An exception raised out of
                # execute_roll may have left a filled BTC behind, so this is an
                # error-severity terminal with the same "look at this" weight as
                # call_roll_naked_exposure, not a benign "no roll today".
                logger.error("Roll execution raised",
                             event_category="error",
                             event_type="roll_position_error",
                             symbol=option_symbol, error=str(exc), exc_info=True)
                log_error_event(
                    logger, error_type="call_roll_execution_error",
                    error_message=str(exc), component="wheel_engine",
                    recoverable=False, symbol=option_symbol,
                    underlying=underlying,
                )
                results['rolls_skipped'] += 1
                continue

            results['roll_details'].append(roll_result)

            if roll_result.get('success'):
                results['rolls_executed'] += 1
            else:
                results['rolls_skipped'] += 1

        duration = (clock.now() - start_time).total_seconds()
        log_system_event(
            logger, event_type="roll_cycle_completed", status="completed",
            rolls_evaluated=results['rolls_evaluated'],
            rolls_executed=results['rolls_executed'],
            rolls_skipped=results['rolls_skipped'],
            duration_seconds=round(duration, 2),
        )

        return results