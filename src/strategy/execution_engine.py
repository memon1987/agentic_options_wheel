"""Execution engine for batch order selection and trade execution.

Extracts business logic from the Flask server layer (cloud_run_server.py /run endpoint)
into independently testable methods:
  - Idempotency checks (filtering opportunities where positions already exist)
  - Buying power tracking
  - ROI-based opportunity ranking
  - Batch order selection
  - Sequential order execution
"""

import re
from typing import Dict, List, Any, Optional, Set, Tuple

import structlog

from ..api.alpaca_client import AlpacaClient
from ..api.market_data import MarketDataManager
from ..data.trade_journal import TradeJournal
from ..utils.config import Config
from ..utils.logging_events import log_system_event, log_error_event
from ..utils.option_symbols import occ_root, parse_option_symbol, strict_option_type
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
# FC-041: the execution-stage belt. Unlike the five above it is NOT raised by
# `select_batch` -- it fires in `execute_batch`, either when the
# parser-independent recount disagrees with `_available_shares` or when a short
# option on the underlying cannot be classified at all. Both mean a defect or a
# corporate action, never ordinary batch contention. It is in this enum because
# it rides the same `selection_dropped` event and must carry a bucket in the
# backtest rejection tally; it is deliberately NOT in `select_batch`'s
# drop-count summary, where it could only ever be a structural zero.
DROP_NAKED_CALL_INVARIANT = "naked_call_invariant"

DROP_REASONS = (
    DROP_INSUFFICIENT_SHARES,
    DROP_INSUFFICIENT_BP,
    DROP_DUPLICATE_UNDERLYING,
    DROP_SIZING_FAILED,
    DROP_POSITIONS_UNAVAILABLE,
    DROP_NAKED_CALL_INVARIANT,
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

        FC-041: both legs meet on :func:`occ_root`. ``underlying`` arrives from
        the scanner as the *equity* symbol (``BRK.B``) and the equity position
        renders the same way, but the short call's parsed underlying is the OCC
        root (``BRKB``). Comparing the raw strings made ``committed`` read 0 for
        every dotted ticker — available == owned — and the engine would write a
        second call over shares already committed. Plain tickers normalize to
        themselves, so nothing else moves.
        """
        if not underlying:
            return 0, 0, 0

        root = occ_root(underlying)
        owned = 0
        committed = 0
        for pos in positions or []:
            try:
                asset_class = pos.get('asset_class')
                if asset_class == 'us_equity':
                    if occ_root(pos.get('symbol')) == root:
                        owned += int(float(pos.get('qty') or 0))
                elif asset_class == 'us_option':
                    qty = float(pos.get('qty') or 0)
                    if qty >= 0:  # only SHORT calls commit shares
                        continue
                    opt_sym = pos.get('symbol') or ''
                    if (parse_option_symbol(opt_sym).get('underlying') == root
                            and strict_option_type(opt_sym) == 'call'):
                        committed += abs(int(qty)) * 100
            except (TypeError, ValueError):
                # A malformed position must never inflate availability.
                continue

        return owned - committed, owned, committed

    @staticmethod
    def _invariant_shares(
        underlying: Optional[str],
        positions: List[Dict[str, Any]],
    ) -> Tuple[int, int, List[str]]:
        """``(owned, committed, unclassifiable)`` — **no** ``parse_option_symbol``.

        FC-041. A deliberately second, independent implementation of the
        arithmetic in :meth:`_available_shares` — belt to that method's braces.
        A single shared helper cannot catch its own bug; two implementations
        that must agree can.

        Independence is the point, so it uses only the two primitives whose
        answers are fully anchored: :func:`strict_option_type` (which proves the
        symbol is an exact ``ROOT+YYMMDD+C/P+STRIKE8`` contract) and
        :func:`occ_root`. Because ``strict_option_type`` has already proven the
        shape, the root is exactly the first ``len - 15`` characters — no
        heuristic, no leading-letters fallback, no regex of its own for the
        classified case.

        **``unclassifiable`` is what makes this gate fail CLOSED.**
        ``strict_option_type`` returns ``None`` for an *adjusted* contract
        (``AAPL1260821C00250000`` — a split or special-dividend root) and for
        anything else off-shape (``AAPL260724C00340000X``). Counting those as
        "commits 0 shares" is exactly the naked write this gate exists to stop:
        an adjusted short call is a real obligation against real shares. So any
        short ``us_option`` this method cannot classify, whose leading-alpha
        prefix normalizes to the target root, is collected here instead, and
        the caller refuses the write. Deliberate consequences:

        - It is **type-blind** on that path. An unclassifiable short *put* on
          the underlying also blocks. We cannot prove it is not a call, and the
          cost of being wrong is one skipped covered call against writing naked.
        - It is **root-prefix scoped**, so unrelated garbage (``NOT_AN_OCC``)
          does not block every underlying in the account.
        - The share arithmetic is deliberately *not* attempted on an adjusted
          contract: its deliverable is not 100 shares, so any number we
          computed would be wrong. Refusing is the only honest answer.

        A non-string ``symbol`` is skipped outright rather than collected: the
        prefix rule cannot tell whether it is even on this underlying, and
        blocking every call in the account on one unreadable field is a blast
        radius out of proportion to a shape Alpaca does not produce. It matches
        :meth:`_available_shares` exactly (which skips it via ``TypeError``),
        and the position still surfaces within the hour as the regression
        monitor's ``risk_unclassifiable_option`` warn finding.

        What this method does NOT see, on purpose: it reads one positions
        snapshot, so a *working order* not yet filled commits nothing here
        (FC-061), and it cannot detect a position that changed between the
        snapshot and the order landing at the exchange.
        """
        if not underlying:
            return 0, 0, []

        root = occ_root(underlying)
        owned = 0
        committed = 0
        unclassifiable: List[str] = []
        for pos in positions or []:
            try:
                asset_class = pos.get('asset_class')
                if asset_class == 'us_equity':
                    if occ_root(pos.get('symbol')) == root:
                        owned += int(float(pos.get('qty') or 0))
                elif asset_class == 'us_option':
                    qty = float(pos.get('qty') or 0)
                    if qty >= 0:
                        continue
                    raw = pos.get('symbol')
                    if not isinstance(raw, str):
                        continue
                    sym = raw.strip().upper()
                    opt_type = strict_option_type(sym)
                    if opt_type is None:
                        prefix = re.match(r'^[A-Z]+', sym)
                        if prefix and occ_root(prefix.group(0)) == root:
                            unclassifiable.append(raw)
                        continue
                    if opt_type != 'call':
                        continue
                    if occ_root(sym[:-15]) == root:
                        committed += abs(int(qty)) * 100
            except (TypeError, ValueError):
                continue

        return owned, committed, unclassifiable

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
        selected_underlyings: Set[str] = set()   # OCC roots, not equity symbols
        remaining_bp = available_buying_power
        # Every reason selection can actually raise. DROP_NAKED_CALL_INVARIANT
        # is excluded on purpose: it belongs to `execute_batch`, which does not
        # run this counter, so a key for it here could only ever report a
        # structural zero -- a field that looks like a control's health metric
        # and is not one. If selection ever tried to raise it, the KeyError is
        # the correct, loud answer.
        drop_counts: Dict[str, int] = {
            reason: 0 for reason in DROP_REASONS
            if reason != DROP_NAKED_CALL_INVARIANT
        }
        selected_counts: Dict[str, int] = {'call': 0, 'put': 0}

        def drop(item: Dict[str, Any], reason: str, **fields: Any) -> None:
            drop_counts[reason] += 1
            self._log_drop(item['opportunity'], reason, stage="selection", **fields)

        def select(item: Dict[str, Any], underlying: Optional[str],
                   key: str, **fields: Any) -> None:
            selected.append(item['opportunity'])
            # The event reports the equity spelling the producer used; the
            # one-position-per-underlying set is keyed on the OCC root
            # (FC-041), so the two never drift apart on a class share.
            selected_underlyings.add(key)
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
            # FC-041: both per-batch keys are the OCC root, so `BRK.B` and
            # `BRKB` are one underlying to the duplicate rule and to the share
            # ledger. Keyed on the raw symbol, two spellings of the same
            # company would each get their own 100-share budget out of the
            # same 100 shares. `select()` still reports the equity spelling.
            key = occ_root(underlying)

            if key in selected_underlyings:
                drop(item, DROP_DUPLICATE_UNDERLYING)
                continue

            if key not in share_ledger:
                share_ledger[key] = self._available_shares(underlying, positions)[0]

            required_shares = int(opp.get('contracts') or 1) * 100
            if share_ledger[key] < required_shares:
                drop(
                    item,
                    self._share_drop_reason(positions),
                    required_shares=required_shares,
                    available_shares=share_ledger[key],
                )
                continue

            share_ledger[key] -= required_shares
            select(
                item,
                underlying,
                key,
                required_shares=required_shares,
                remaining_shares=share_ledger[key],
                remaining_bp=remaining_bp,
            )

        # --- Pool 2: cash-secured puts, against buying power ---
        for item in puts:
            opp = item['opportunity']
            underlying = opp.get('symbol')
            key = occ_root(underlying)

            if key in selected_underlyings:
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
                key,
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
            # FC-041's `naked_call_invariant` is deliberately absent: it fires
            # in `execute_batch`, after selection is over, so a field here
            # could only ever be a structural zero. Its real signals are the
            # `naked_call_invariant_blocked` error event and
            # `selection_dropped{reason=naked_call_invariant, stage=execution}`.
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

                    # FC-041 — the parser-independent invariant. Last thing
                    # before the order goes out.
                    #
                    # _available_shares just answered "enough". This recounts
                    # the SAME snapshot with a different implementation
                    # (`_invariant_shares`: strict_option_type + occ_root only,
                    # no parse_option_symbol) and requires
                    #     committed + about-to-sell <= owned.
                    # If the two disagree, one of them is wrong and we do not
                    # know which — so we fail closed and say so loudly. Silence
                    # here is a naked call; a false positive here is one
                    # skipped covered call.
                    inv_owned, inv_committed, inv_unclassifiable = (
                        self._invariant_shares(underlying, snapshot)
                    )
                    if (inv_unclassifiable
                            or inv_committed + required_shares > inv_owned):
                        log_error_event(
                            self.logger,
                            error_type="naked_call_invariant_blocked",
                            error_message=(
                                f"Naked-call invariant violated for {underlying}: "
                                f"committed {inv_committed} + requested "
                                f"{required_shares} vs owned {inv_owned}, "
                                f"unclassifiable short options "
                                f"{inv_unclassifiable}; refusing to write the call"
                            ),
                            component="execution_engine",
                            recoverable=True,
                            symbol=underlying,
                            option_symbol=opp.get('option_symbol'),
                            owned=inv_owned,
                            committed=inv_committed,
                            requested=required_shares,
                            # Which limb fired. A non-empty list means the
                            # arithmetic was never reached: there is a short
                            # option on this underlying we refuse to classify.
                            unclassifiable_short_options=inv_unclassifiable,
                            # What the primary ledger said, so the disagreement
                            # itself is in the record and diagnosable.
                            available_shares_helper=available_shares,
                            owned_shares_helper=owned_shares,
                            committed_shares_helper=committed_shares,
                            positions_unavailable=isinstance(
                                snapshot, PositionsUnavailable),
                        )
                        self._log_drop(
                            opp,
                            DROP_NAKED_CALL_INVARIANT,
                            stage="execution",
                            owned_shares=inv_owned,
                            committed_to_calls=inv_committed,
                            required_shares=required_shares,
                            unclassifiable_short_options=inv_unclassifiable,
                        )
                        # Not added to _failed_symbols: like naked_call_blocked,
                        # share ownership is transient and the same contract can
                        # be legitimate next cycle.
                        execution_results.append({
                            'opportunity': opp,
                            'result': {
                                'success': False,
                                'error_type': 'naked_call_invariant',
                                'message': (
                                    f'Call blocked by naked-call invariant: '
                                    f'{inv_committed} committed + {required_shares} '
                                    f'requested vs {inv_owned} owned, '
                                    f'unclassifiable {inv_unclassifiable}'
                                ),
                            },
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
                        # FC-072: `quote_source` has no column on `trades` and
                        # deliberately does not get one — a new column on the
                        # canonical table for a provenance flag is not worth the
                        # schema migration. It rides the event instead, joinable
                        # to the row by order_id.
                        quote_source=result.get("quote_source"),
                        quote_age_s=result.get("quote_age_s"),
                        tick_snapped=result.get("tick_snapped"),
                    )

                    # Persist trade to BigQuery journal.
                    #
                    # FC-072: the quote columns come from the RESULT, not from
                    # `opp`. `**opp` carries the :00 scan blob's bid/ask, and
                    # since FC-072 the limit is priced off a quote refreshed at
                    # :15 — so a row built from the opportunity alone showed a
                    # live-priced limit sitting next to a 15-minute-old book,
                    # and `mid_price` was NULL because no opportunity ever had
                    # that key. Any "where did we price relative to the market"
                    # analysis read the wrong book. The sellers return the book
                    # they actually priced off; it wins here.
                    #
                    # `.get(..., opp.get(...))` rather than a bare `.get`: a
                    # seller that predates FC-072, or any future producer that
                    # does not carry the fields, degrades to the old behaviour
                    # instead of writing NULLs over a usable scan-time quote.
                    self.trade_journal.record_trade({
                        **opp,
                        "order_id": order_id,
                        "client_order_id": str(result.get("client_order_id")) if result.get("client_order_id") else None,
                        "status": "submitted",
                        "fill_price": result.get("fill_price"),
                        "limit_price": result.get("limit_price"),
                        "bid": result.get("bid", opp.get("bid")),
                        "ask": result.get("ask", opp.get("ask")),
                        "mid_price": result.get("mid"),
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
