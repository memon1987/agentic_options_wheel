"""Execution engine for batch order selection and trade execution.

Extracts business logic from the Flask server layer (cloud_run_server.py /run endpoint)
into independently testable methods:
  - Idempotency checks (filtering opportunities where positions already exist)
  - Buying power tracking
  - ROI-based opportunity ranking
  - Batch order selection
  - Sequential order execution
"""

from typing import Dict, List, Any, Optional, Set, Tuple
import structlog

from ..api.alpaca_client import AlpacaClient
from ..api.market_data import MarketDataManager
from ..data.trade_journal import TradeJournal
from ..utils.config import Config
from ..utils.logging_events import log_system_event, log_error_event
from ..utils.option_symbols import parse_option_symbol, strict_option_type
from .put_seller import PutSeller
from .call_seller import CallSeller

logger = structlog.get_logger(__name__)

# Machine-queryable drop reasons emitted on the `selection_dropped` event.
# Exhaustive: every opportunity that enters ranking either ends up selected or
# carries exactly one of these.
DROP_INSUFFICIENT_SHARES = "insufficient_available_shares"
DROP_INSUFFICIENT_BP = "insufficient_buying_power"
DROP_DUPLICATE_UNDERLYING = "duplicate_underlying"
DROP_SIZING_FAILED = "sizing_failed"
# Distinct from insufficient_available_shares on purpose: a failed positions
# fetch must never masquerade as "the account owns no shares". The two demand
# opposite responses -- one is a normal, expected outcome, the other is an
# outage that silently halts every covered call in the fleet.
DROP_POSITIONS_UNAVAILABLE = "positions_unavailable"

DROP_REASONS = (
    DROP_INSUFFICIENT_SHARES,
    DROP_INSUFFICIENT_BP,
    DROP_DUPLICATE_UNDERLYING,
    DROP_SIZING_FAILED,
    DROP_POSITIONS_UNAVAILABLE,
)


class PositionsUnavailable(list):
    """An empty positions snapshot whose emptiness means "we could not look".

    Behaves as ``[]`` everywhere, so every share computation fails closed
    (nothing available, nothing sold naked). Callers that care about *why* the
    snapshot is empty test for this type and report
    ``positions_unavailable`` rather than ``insufficient_available_shares``.
    """

    __slots__ = ()

# Module-level set tracking option symbols that failed with non-retryable errors today.
# This prevents the same rejected opportunity from being retried every /run cycle.
# Cleared on service restart (daily Cloud Run cold start).
_failed_symbols: Set[str] = set()


def get_failed_symbols() -> Set[str]:
    """Return the current set of failed (non-retryable) option symbols."""
    return _failed_symbols


def clear_failed_symbols() -> None:
    """Clear the failed symbols set (e.g., at start of new trading day)."""
    _failed_symbols.clear()


class ExecutionEngine:
    """Handles batch opportunity selection and trade execution.

    Encapsulates the business logic previously inlined in the ``/run``
    endpoint of ``cloud_run_server.py``.
    """

    def __init__(self, alpaca_client: AlpacaClient, config: Config,
                 log: Optional[Any] = None,
                 trade_journal: Optional[TradeJournal] = None):
        """Initialize the execution engine.

        Args:
            alpaca_client: Alpaca API client instance.
            config: Application configuration.
            log: Optional structlog logger (falls back to module logger).
            trade_journal: Optional TradeJournal for persistent trade recording.
        """
        self.alpaca_client = alpaca_client
        self.config = config
        self.logger = log or logger
        # The journal's dataset comes from THIS process's profile (FC-075 Seam
        # 4) — never a writer-side default, which is how a second profile would
        # journal into the wheel's trades table.
        self.trade_journal = trade_journal or TradeJournal(
            dataset_id=config.bigquery_dataset,
            strategy_id=config.strategy_id,
        )
        # FC-065 Phase 4: underlying -> the reason its last CALL opportunity
        # was dropped this cycle. A read-only side channel for the decision
        # record, populated at the single `_log_drop` chokepoint so ranking
        # and selection cannot diverge from it. Last write wins: the later
        # stage is the more specific verdict. Cleared per cycle by
        # `rank_opportunities`, which always runs first on the /run path.
        #
        # CALLS ONLY, and the name says so. Only one position per underlying is
        # allowed across both pools, so a call selected on AAPL drops the AAPL
        # put as `duplicate_underlying` — a symbol-keyed map holding both would
        # hand the covered-call decision record the PUT's reason.
        self.last_call_drop_reasons: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _declared_type(opportunity: Dict[str, Any]) -> Optional[str]:
        """``'put'``/``'call'`` from the producer's own keys, else ``None``.

        The scanner sets ``type``; the sellers set ``strategy`` (FC-048).
        """
        declared = opportunity.get('type')
        if declared in ('put', 'call'):
            return declared
        strategy = opportunity.get('strategy')
        if strategy == 'sell_call':
            return 'call'
        if strategy == 'sell_put':
            return 'put'
        return None

    @classmethod
    def _opportunity_type(cls, opportunity: Dict[str, Any]) -> str:
        """Classify an opportunity the same way ``execute_batch`` routes it.

        The OCC symbol wins over any declared key — it is what
        ``place_option_order`` actually trades. Unparseable symbols fall back to
        the declared key and finally to ``'put'``; ``execute_batch`` refuses
        them outright, so nothing untradeable escapes on this path.
        """
        return (
            strict_option_type(opportunity.get('option_symbol') or '')
            or cls._declared_type(opportunity)
            or 'put'
        )

    @staticmethod
    def _available_shares(
        underlying: Optional[str],
        positions: List[Dict[str, Any]],
    ) -> Tuple[int, int, int]:
        """Shares of ``underlying`` free to back a *new* covered call.

        Returns ``(available, owned, committed)``.  ``committed`` counts only
        genuine short CALL contracts on this underlying, parsed with the
        canonical primitives — ``'C' in symbol`` was the fifth member of the
        OCC-substring bug family (FC-041/043/045/048/052).

        Single implementation shared by call sizing, batch selection and the
        execution-time oversell guard.
        """
        if not underlying:
            return 0, 0, 0

        owned = 0
        committed = 0
        for pos in positions or []:
            try:
                asset_class = pos.get('asset_class')
                if asset_class == 'us_equity':
                    if pos.get('symbol') == underlying:
                        owned += int(float(pos.get('qty') or 0))
                elif asset_class == 'us_option':
                    qty = float(pos.get('qty') or 0)
                    if qty >= 0:  # only SHORT calls commit shares
                        continue
                    opt_sym = pos.get('symbol') or ''
                    if (parse_option_symbol(opt_sym).get('underlying') == underlying
                            and strict_option_type(opt_sym) == 'call'):
                        committed += abs(int(qty)) * 100
            except (TypeError, ValueError):
                # A malformed position must never inflate availability.
                continue

        return owned - committed, owned, committed

    def _positions_snapshot(
        self,
        positions: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """Return the caller's snapshot, or fetch one.

        Fails closed: a failed fetch yields a :class:`PositionsUnavailable`,
        which is an empty list *and* carries the reason it is empty.
        """
        if positions is not None:
            return positions
        try:
            return self.alpaca_client.get_positions() or []
        except Exception as e:
            self.logger.warning(
                "Could not fetch positions — treating share availability as zero",
                event_category="system",
                event_type="positions_snapshot_failed",
                error=str(e),
            )
            return PositionsUnavailable()

    @staticmethod
    def _share_drop_reason(positions: List[Dict[str, Any]]) -> str:
        """Why a call could not be backed: no shares, or no snapshot at all."""
        return (DROP_POSITIONS_UNAVAILABLE
                if isinstance(positions, PositionsUnavailable)
                else DROP_INSUFFICIENT_SHARES)

    @classmethod
    def _call_rank_score(cls, item: Dict[str, Any]) -> float:
        """Ranking key for the call pool.

        Covered calls have zero collateral, so the put pool's
        ``premium / collateral`` ROI collapses to 0 and would sort every call
        last. Rank them by the scanner's ``attractiveness_score`` instead,
        falling back to premium yield on the notional it covers.
        """
        opp = item.get('opportunity', {})
        score = opp.get('attractiveness_score')
        try:
            if score is not None:
                return float(score)
        except (TypeError, ValueError):
            pass

        try:
            strike = float(opp.get('strike_price') or 0)
            premium = float(opp.get('premium') or 0)
        except (TypeError, ValueError):
            return 0.0
        return premium / (strike * 100) if strike > 0 else 0.0

    def _log_drop(
        self,
        opportunity: Dict[str, Any],
        reason: str,
        stage: str,
        **fields: Any,
    ) -> None:
        """Record why an opportunity will not be traded this cycle.

        The 2026-07-18 starvation outage was invisible for four days precisely
        because drops were silent (see docs/investigations/).

        Premium is reported in both units explicitly. A bare ``premium`` field
        meant per-share here and total-dollars on the selection event, so any
        query spanning the two silently mixed scales.
        """
        per_share = opportunity.get('premium')
        contracts = opportunity.get('contracts')
        total = (per_share * 100 * contracts
                 if per_share is not None and contracts else None)

        underlying = opportunity.get('symbol')
        if underlying and self._opportunity_type(opportunity) == 'call':
            self.last_call_drop_reasons[underlying] = reason

        self.logger.info(
            "Opportunity dropped before execution",
            event_category="filtering",
            event_type="selection_dropped",
            stage=stage,
            reason=reason,
            symbol=opportunity.get('symbol'),
            option_symbol=opportunity.get('option_symbol'),
            opportunity_type=self._opportunity_type(opportunity),
            strike_price=opportunity.get('strike_price'),
            contracts=contracts,
            premium_per_share=per_share,
            total_premium=total,
            **fields,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def filter_duplicate_opportunities(
        self,
        opportunities: List[Dict[str, Any]],
        existing_positions: List[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Remove opportunities where a position already exists (idempotency).

        Args:
            opportunities: Raw opportunities from the opportunity store.
            existing_positions: Current option positions from Alpaca.

        Returns:
            A tuple of (filtered_opportunities, filtered_count).
        """
        existing_symbols: Set[str] = {
            pos.get('symbol') for pos in existing_positions if pos.get('symbol')
        }

        original_count = len(opportunities)
        filtered = [
            opp for opp in opportunities
            if opp.get('option_symbol') not in existing_symbols
        ]

        filtered_count = original_count - len(filtered)
        if filtered_count > 0:
            self.logger.warning(
                "Idempotency check: filtered out opportunities with existing positions",
                event_category="system",
                event_type="idempotency_filter_applied",
                original_count=original_count,
                filtered_count=filtered_count,
                remaining_count=len(filtered),
            )

        return filtered, filtered_count

    def filter_failed_opportunities(
        self,
        opportunities: List[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Remove opportunities whose option symbol previously failed with a non-retryable error.

        Args:
            opportunities: Opportunities to filter.

        Returns:
            A tuple of (filtered_opportunities, filtered_count).
        """
        if not _failed_symbols:
            return opportunities, 0

        original_count = len(opportunities)
        filtered = [
            opp for opp in opportunities
            if opp.get('option_symbol') not in _failed_symbols
        ]

        filtered_count = original_count - len(filtered)
        if filtered_count > 0:
            self.logger.warning(
                "Filtered out previously failed (non-retryable) opportunities",
                event_category="system",
                event_type="non_retryable_filter_applied",
                original_count=original_count,
                filtered_count=filtered_count,
                remaining_count=len(filtered),
                failed_symbols=list(_failed_symbols),
            )

        return filtered, filtered_count

    def rank_opportunities(
        self,
        opportunities: List[Dict[str, Any]],
        put_seller: PutSeller,
        available_buying_power: float,
        positions: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """Size every opportunity and return it ranked, by type.

        Sizing is type-aware (FC-038):

        - **Puts** are cash-secured, so they are sized against buying power by
          ``put_seller._calculate_position_size`` — unchanged.
        - **Calls** are covered by shares, not cash. They are sized as
          ``available_shares // 100`` and never touch buying power. Routing
          calls through the put sizer was the starvation bug: its BP cap
          (``buying_power // (strike * 100)``) silently dropped every call
          whenever cash was low, however well the call scored.

        Each entry in the returned list is a dict with keys ``opportunity``,
        ``collateral``, ``premium``, ``roi``, ``type``. Calls sort first (they
        consume no cash) by ``attractiveness_score``; puts follow by ROI.

        Args:
            opportunities: Opportunities to rank.
            put_seller: PutSeller instance (used for put position sizing).
            available_buying_power: Current buying power for put sizing.
            positions: Optional positions snapshot; fetched when omitted and
                at least one call opportunity is present.

        Returns:
            List of metric dicts, calls first then puts.
        """
        # FC-065 Phase 4: one cycle's drop reasons, and only one cycle's. The
        # engine is constructed per request today, but a reused instance must
        # not report last hour's reason as this hour's decision.
        self.last_call_drop_reasons = {}

        typed = [(opp, self._opportunity_type(opp)) for opp in opportunities]

        if positions is None:
            positions = (
                self._positions_snapshot()
                if any(t == 'call' for _, t in typed)
                else []
            )

        opportunities_with_metrics: List[Dict[str, Any]] = []

        for opp, opp_type in typed:
            # Transform scanner format to position sizing format
            if 'premium' in opp and 'mid_price' not in opp:
                opp['mid_price'] = opp['premium']

            if opp_type == 'call':
                available, owned, committed = self._available_shares(
                    opp.get('symbol'), positions
                )
                contracts = available // 100
                if contracts <= 0:
                    self._log_drop(
                        opp,
                        self._share_drop_reason(positions),
                        stage="ranking",
                        owned_shares=owned,
                        committed_to_calls=committed,
                        available_shares=available,
                    )
                    continue

                opp['contracts'] = contracts
                collateral = 0.0  # covered by shares; no cash is reserved
                premium_collected = opp['premium'] * 100 * contracts
                roi = 0.0  # meaningless against zero collateral — see _call_rank_score
            else:
                position_size = put_seller._calculate_position_size(
                    opp, override_buying_power=available_buying_power
                )
                if not position_size:
                    self._log_drop(
                        opp,
                        DROP_SIZING_FAILED,
                        stage="ranking",
                        buying_power=available_buying_power,
                    )
                    continue

                opp['contracts'] = position_size['contracts']
                collateral = opp['strike_price'] * 100 * opp['contracts']
                premium_collected = opp['premium'] * 100 * opp['contracts']
                roi = premium_collected / collateral if collateral > 0 else 0

            opportunities_with_metrics.append({
                'opportunity': opp,
                'collateral': collateral,
                'premium': premium_collected,
                'roi': roi,
                'type': opp_type,
            })

        return self._sort_pools(opportunities_with_metrics)

    @classmethod
    def _sort_pools(
        cls,
        items: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Calls (by attractiveness) ahead of puts (by ROI).

        Calls consume no cash, so putting them first cannot starve the put
        pool; it just removes any order-dependence between the two.
        """
        calls = [i for i in items if cls._item_type(i) == 'call']
        puts = [i for i in items if cls._item_type(i) != 'call']
        calls.sort(key=cls._call_rank_score, reverse=True)
        puts.sort(key=lambda i: i.get('roi', 0), reverse=True)
        return calls + puts

    @classmethod
    def _item_type(cls, item: Dict[str, Any]) -> str:
        """Type of a ranked metric dict, tolerating hand-built input."""
        declared = item.get('type')
        if declared in ('put', 'call'):
            return declared
        return cls._opportunity_type(item.get('opportunity', {}))

    def select_batch(
        self,
        ranked_opportunities: List[Dict[str, Any]],
        available_buying_power: float,
        positions: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[List[Dict[str, Any]], float]:
        """Select opportunities from two independent budgets (FC-038).

        Covered calls are backed by shares, puts by cash, so they are selected
        against separate ledgers: calls draw down a per-underlying available-
        shares budget, puts draw down buying power. Charging calls
        ``strike x 100`` of phantom collateral is what let one call exhaust the
        cash budget and starve every other opportunity in the batch.

        Calls are selected first — they cost no cash, so they cannot displace a
        put. Only one position per underlying is allowed, across both pools
        (risk management rule). Share-committed underlyings are dropped here
        rather than discovered at execution time, so they no longer burn a slot.

        Args:
            ranked_opportunities: Ranked metric dicts (from ``rank_opportunities``).
            available_buying_power: Starting buying power (puts only).
            positions: Optional positions snapshot; fetched when omitted and
                at least one call opportunity is present.

        Returns:
            A tuple of (selected_opportunities, remaining_buying_power).
        """
        selected: List[Dict[str, Any]] = []
        selected_underlyings: Set[str] = set()
        remaining_bp = available_buying_power
        drop_counts: Dict[str, int] = {reason: 0 for reason in DROP_REASONS}
        selected_counts: Dict[str, int] = {'call': 0, 'put': 0}

        def drop(item: Dict[str, Any], reason: str, **fields: Any) -> None:
            drop_counts[reason] += 1
            self._log_drop(item['opportunity'], reason, stage="selection", **fields)

        def select(item: Dict[str, Any], underlying: Optional[str], **fields: Any) -> None:
            selected.append(item['opportunity'])
            selected_underlyings.add(underlying)
            selected_counts['call' if self._item_type(item) == 'call' else 'put'] += 1
            self.logger.info(
                "Selected opportunity for batch execution",
                event_category="system",
                event_type="opportunity_selected",
                symbol=underlying,
                option_symbol=item['opportunity'].get('option_symbol'),
                opportunity_type=self._item_type(item),
                collateral=item.get('collateral', 0),
                contracts=item['opportunity'].get('contracts'),
                # Both units, named. See _log_drop.
                premium_per_share=item['opportunity'].get('premium'),
                total_premium=item.get('premium'),
                **fields,
            )

        ordered = self._sort_pools(ranked_opportunities)
        calls = [i for i in ordered if self._item_type(i) == 'call']
        puts = [i for i in ordered if self._item_type(i) != 'call']

        if positions is None:
            positions = self._positions_snapshot() if calls else []

        # --- Pool 1: covered calls, against a per-underlying share ledger ---
        share_ledger: Dict[str, int] = {}
        for item in calls:
            opp = item['opportunity']
            underlying = opp.get('symbol')

            if underlying in selected_underlyings:
                drop(item, DROP_DUPLICATE_UNDERLYING)
                continue

            if underlying not in share_ledger:
                share_ledger[underlying] = self._available_shares(underlying, positions)[0]

            required_shares = int(opp.get('contracts') or 1) * 100
            if share_ledger[underlying] < required_shares:
                drop(
                    item,
                    self._share_drop_reason(positions),
                    required_shares=required_shares,
                    available_shares=share_ledger[underlying],
                )
                continue

            share_ledger[underlying] -= required_shares
            select(
                item,
                underlying,
                required_shares=required_shares,
                remaining_shares=share_ledger[underlying],
                remaining_bp=remaining_bp,
            )

        # --- Pool 2: cash-secured puts, against buying power ---
        for item in puts:
            opp = item['opportunity']
            underlying = opp.get('symbol')

            if underlying in selected_underlyings:
                drop(item, DROP_DUPLICATE_UNDERLYING)
                continue

            collateral = item.get('collateral', 0)
            if collateral > remaining_bp:
                drop(
                    item,
                    DROP_INSUFFICIENT_BP,
                    collateral=collateral,
                    remaining_bp=remaining_bp,
                )
                continue

            remaining_bp -= collateral
            select(
                item,
                underlying,
                roi=f"{item.get('roi', 0):.4f}",
                remaining_bp=remaining_bp,
            )

        self.logger.info(
            "Batch order selection complete",
            event_category="system",
            event_type="batch_selection_completed",
            total_opportunities=len(ranked_opportunities),
            selected_count=len(selected),
            calls_selected=selected_counts['call'],
            puts_selected=selected_counts['put'],
            dropped_count=sum(drop_counts.values()),
            dropped_insufficient_available_shares=drop_counts[DROP_INSUFFICIENT_SHARES],
            dropped_insufficient_buying_power=drop_counts[DROP_INSUFFICIENT_BP],
            dropped_duplicate_underlying=drop_counts[DROP_DUPLICATE_UNDERLYING],
            dropped_positions_unavailable=drop_counts[DROP_POSITIONS_UNAVAILABLE],
            initial_bp=available_buying_power,
            bp_to_use=available_buying_power - remaining_bp,
        )

        return selected, remaining_bp

    def execute_batch(
        self,
        selected_opportunities: List[Dict[str, Any]],
        put_seller: PutSeller,
        call_seller: Optional[CallSeller] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Execute orders sequentially with real-time buying power validation.

        Routes put opportunities to put_seller and call opportunities to
        call_seller.  Call opportunities are rejected if call_seller is not
        provided or if the account does not own sufficient shares (naked call
        prevention).

        Args:
            selected_opportunities: Opportunities chosen by ``select_batch``.
            put_seller: PutSeller instance for put order execution.
            call_seller: Optional CallSeller instance for covered call execution.

        Returns:
            A tuple of (execution_results, trades_executed_count).
        """
        self.logger.info(
            "Executing batch orders sequentially",
            event_category="system",
            event_type="batch_orders_executing",
            order_count=len(selected_opportunities),
        )

        execution_results: List[Dict[str, Any]] = []
        trades_executed = 0

        for opp in selected_opportunities:
            try:
                # FC-048: route on the OCC symbol, not on a dict key.
                #
                # This previously read `opp.get('type', 'put')`. Only the
                # scanner sets 'type'; the sellers set 'strategy' instead, so
                # every seller-produced covered call defaulted to "put", was
                # handed to put_seller, and was rejected by its wrong-seller
                # guard. That is why every backtest modelled a put-only wheel.
                #
                # The OCC symbol is the one field every producer sets and the
                # only one that cannot drift from the order actually placed, so
                # routing on it removes both the missing-key failure (this bug)
                # and any future wrong-key failure. A missing/garbage symbol
                # now fails LOUD rather than silently trading as a put --
                # the permissive default was the trap.
                #
                # STRICT parse deliberately: parse_option_symbol has a
                # last-resort `'C' if 'C' in symbol` heuristic that resolves a
                # bare 'AAPL' to "put" and 'NOT_AN_OCC' to "call". Routing on
                # that would hand a non-contract to a seller, and
                # place_option_order on a bare ticker is a plain EQUITY order.
                # Adjusted roots ('1AAPL...') are also refused: their
                # deliverable is not 100 shares, so the collateral and
                # cost-basis math would be wrong.
                option_symbol = opp.get('option_symbol') or ''
                opp_type = strict_option_type(option_symbol)

                declared = opp.get('type') or (
                    'call' if opp.get('strategy') == 'sell_call'
                    else 'put' if opp.get('strategy') == 'sell_put' else None
                )
                if declared and opp_type in ('put', 'call') and declared != opp_type:
                    # Trust the contract: it is what place_option_order trades.
                    self.logger.warning(
                        "Opportunity type contradicts its OCC symbol - routing by symbol",
                        event_category="trade",
                        event_type="opportunity_type_mismatch",
                        symbol=opp.get('symbol'),
                        option_symbol=option_symbol,
                        declared_type=declared,
                        parsed_type=opp_type,
                    )

                if opp_type is None:
                    log_error_event(
                        self.logger,
                        error_type="unroutable_opportunity",
                        error_message=(
                            f"Cannot determine option type from option_symbol="
                            f"{option_symbol!r}; refusing to execute"
                        ),
                        component="execution_engine",
                        recoverable=False,
                        symbol=opp.get('symbol'),
                        option_symbol=option_symbol,
                    )
                    execution_results.append({
                        'opportunity': opp,
                        'result': {
                            'success': False,
                            'error_type': 'unroutable_opportunity',
                            'message': (
                                f"Unroutable opportunity: option_symbol="
                                f"{option_symbol!r} is not a valid OCC contract symbol"
                            ),
                            'non_retryable': True,
                        },
                        'success': False,
                    })
                    # Suppress the retry storm: without this the same malformed
                    # opportunity is re-processed and re-logged every cycle until
                    # the scan expires. Keyed on option_symbol (module-global,
                    # matching the non-retryable path below) -- NOT on the
                    # underlying, which would blacklist every future legitimate
                    # contract on that ticker.
                    if option_symbol:
                        _failed_symbols.add(option_symbol)
                    continue

                if opp_type == 'call':
                    # --- Covered call path ---
                    if call_seller is None:
                        self.logger.warning(
                            "Call opportunity skipped — no call_seller provided",
                            event_category="trade",
                            event_type="call_skipped_no_seller",
                            symbol=opp.get('symbol'),
                            option_symbol=opp.get('option_symbol'),
                        )
                        execution_results.append({
                            'opportunity': opp,
                            'result': {'success': False, 'message': 'No call seller available'},
                            'success': False,
                        })
                        continue

                    # Verify AVAILABLE shares before executing covered call.
                    # Available = owned shares - shares already committed to existing short calls.
                    underlying = opp.get('symbol')
                    contracts = opp.get('contracts', 1)
                    required_shares = contracts * 100

                    # FC-038: same arithmetic the sizing and selection stages
                    # use, via one shared helper. Kept here as defence in depth
                    # — positions can change between selection and execution —
                    # so a firing is now itself a signal worth its warning log.
                    snapshot = self._positions_snapshot()
                    available_shares, owned_shares, committed_shares = self._available_shares(
                        underlying, snapshot
                    )

                    if available_shares < required_shares:
                        self.logger.warning(
                            "Call blocked — insufficient available shares",
                            event_category="risk",
                            event_type="naked_call_blocked",
                            symbol=underlying,
                            option_symbol=opp.get('option_symbol'),
                            required_shares=required_shares,
                            owned_shares=owned_shares,
                            committed_to_calls=committed_shares,
                            available_shares=available_shares,
                            # "0 owned" reads very differently when it means
                            # "we could not fetch positions".
                            positions_unavailable=isinstance(snapshot, PositionsUnavailable),
                        )
                        # Do NOT add to _failed_symbols — share ownership is transient.
                        # The account could acquire shares before the next /run cycle
                        # (e.g., put assignment), at which point this call becomes valid.
                        execution_results.append({
                            'opportunity': opp,
                            'result': {'success': False, 'message': f'Call blocked: {available_shares} available shares ({owned_shares} owned - {committed_shares} committed), need {required_shares}'},
                            'success': False,
                        })
                        continue

                    result = call_seller.execute_call_sale(opp)
                else:
                    # --- Put path (default) ---
                    result = put_seller.execute_put_sale(opp, skip_buying_power_check=False)

                execution_data = {
                    'opportunity': opp,
                    'result': result,
                    'success': result and result.get('success', False),
                }
                execution_results.append(execution_data)

                if execution_data['success']:
                    trades_executed += 1

                    # Convert UUID to string if present
                    order_id = result.get('order_id')
                    if order_id is not None:
                        order_id = str(order_id)

                    log_system_event(
                        self.logger,
                        event_type="trade_executed",
                        status="success",
                        symbol=opp.get('symbol'),
                        option_symbol=opp.get('option_symbol'),
                        contracts=opp.get('contracts'),
                        premium=opp.get('premium'),
                        strike_price=opp.get('strike_price'),
                        order_id=order_id,
                    )

                    # Persist trade to BigQuery journal
                    self.trade_journal.record_trade({
                        **opp,
                        "order_id": order_id,
                        "client_order_id": str(result.get("client_order_id")) if result.get("client_order_id") else None,
                        "status": "submitted",
                        "fill_price": result.get("fill_price"),
                        "limit_price": result.get("limit_price"),
                    })
                else:
                    error_message = result.get('message', '') or result.get('error_message', 'Unknown error')
                    is_non_retryable = result.get('non_retryable', False)

                    # Track non-retryable failures so they are skipped in future cycles
                    if is_non_retryable:
                        option_sym = opp.get('option_symbol')
                        if option_sym:
                            _failed_symbols.add(option_sym)
                            self.logger.warning(
                                "Marking opportunity as non-retryable failure",
                                event_category="system",
                                event_type="opportunity_marked_non_retryable",
                                option_symbol=option_sym,
                                error_type=result.get('error_type', 'unknown'),
                                error_message=error_message,
                            )

                    log_error_event(
                        self.logger,
                        error_type="trade_execution_failed",
                        error_message=error_message,
                        component="execution_engine",
                        recoverable=not is_non_retryable,
                        symbol=opp.get('symbol'),
                        option_symbol=opp.get('option_symbol'),
                    )
            except Exception as e:
                self.logger.error(
                    "Exception during order execution",
                    event_category="error",
                    event_type="order_execution_exception",
                    symbol=opp.get('symbol'),
                    error=str(e),
                )
                execution_results.append({
                    'opportunity': opp,
                    'result': {'success': False, 'message': str(e)},
                    'success': False,
                })

        return execution_results, trades_executed
