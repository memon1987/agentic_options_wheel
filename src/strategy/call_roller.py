"""Covered call rolling engine — daily, credit-only defensive rolls.

Plan: docs/plans/fc-078.md (revives FC-006, which never executed a roll in
production; FC-066 diagnosed the four stacked causes).

Buys-to-close an ITM short call and sells-to-open a higher strike further out,
as two sequential single-leg limit orders, **BTC first**. The whole design turns
on one invariant:

    STO_limit - BTC_limit >= rolling.min_net_credit_per_contract / 100

checked on the *limit prices actually placed*, at four sites: the candidate
screen, immediately before BTC placement, order construction, and every ladder
rung. Because both legs are limit orders, fills can only improve on it (BTC
fills at <= limit, STO at >= limit), so **a filled roll can never net a debit**.
There is no debit path to tolerate, so there is no debit tolerance — and no
dependency on a wheel-state ``original_premium`` that has never persisted.

Terminal-event contract (FC-078 DD-5): every short call evaluated in a cycle
emits **exactly one** terminal event, one of:

    call_roll_skipped{skip_reason}   no order was ever placed
    call_roll_btc_rejected           the BTC was refused; nothing live
    call_roll_btc_timeout_canceled   canceled with VERIFIED zero fill
    call_roll_naked_exposure         some BTC qty filled, STO ladder exhausted
    call_roll_completed              both legs done
    call_roll_dry_run                ROLLER_DRY_RUN=true; nothing placed

``call_roll_leg_settled`` (FC-120 PR-1) is a per-LEG row — exactly one per
placed order, at that order's disposition — and is deliberately NOT part of
this contract: a position that placed a BTC and two STO rungs emits three of
them and still exactly one terminal. ``call_roll_stc_timeout_canceled`` is
per-rung for the same reason. Both are informational and never alert-wired.

Events must tell the truth about what filled. A cancel that fails *because the
order filled* is a fill, not an error — which is why every cancel on this path
is followed by a re-fetch before anything is reported.
"""

import math
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Callable, Dict, Any, Iterator, List, Optional, Set, Tuple

import structlog

from ..api.alpaca_client import AlpacaClient
from ..api.market_data import MarketDataManager
from ..api.earnings_calendar import (
    EarningsCalendarService, EARNINGS_KNOWN, EARNINGS_UNKNOWN)
from ..risk.risk_manager import RiskManager
from ..utils.config import Config
from ..utils.option_symbols import parse_option_symbol, coerce_expiry_date
from ..utils.logging_events import log_trade_event, log_error_event, log_position_update
from ..utils import clock
from .cost_basis import CostBasisResolver, SOURCE_DIVERGENT
# FC-120 PR-1: logging helpers only — the roller's limits are NOT routed through
# this module (see the docstring of ``_quote_fields``).
from .limit_pricing import _dec, tick_size, quote_age_seconds

logger = structlog.get_logger(__name__)

# Float slack on the credit comparison. Limits are rounded to cents before the
# invariant is tested, so this only absorbs binary-representation noise on an
# exactly-at-the-floor roll (the default floor is $0.00, which is exactly the
# case that must not be rejected by 1e-17).
_CREDIT_EPSILON = 1e-9

# Half-spread crossed in imminence mode, per share. Symmetric on both legs so
# the invariant is tested against a like-for-like pair.
_IMMINENCE_PAD = 0.05

# Stock-quote spread sanity bound. A live IEX example that motivated it: AAPL
# bid 287.82 / ask 318.75 — a 10.7% spread that makes the ITM ratio 0.92 or 1.02
# depending on which side you trust. A quote that wide must not gate a money
# decision.
_MAX_STOCK_SPREAD_RATIO = 1.05

# Statuses at which an order is done moving on its own. ``partially_filled`` is
# deliberately absent: it is NOT terminal, and returning on it (as the as-built
# code did) leaves the remainder working while the next leg is placed.
#
# ``pending_cancel`` is absent for a sharper reason (FC-078 review, execution
# H-1): Alpaca cancels are QUEUED, not synchronous. An order sitting in
# ``pending_cancel`` is still working and can still fill. Treating that read as
# a disposition is how a roller reports "canceled, zero fill" about contracts it
# is in the middle of buying.
_TERMINAL_ORDER_STATUSES = ('filled', 'expired', 'canceled', 'rejected')

# How long to keep re-reading an order after cancelling it, waiting for the
# broker to settle it into a terminal status. Short: a cancel that has not
# resolved in this window is not going to tell us anything by waiting longer,
# and the safe action does not depend on the answer — we stop touching the
# position either way.
_CANCEL_SETTLE_TIMEOUT_SECONDS = 15

# FC-120 PR-1 (ruling B): hard wall-clock cap on EVERY instrumentation read.
# The read runs in a daemon worker; a read that has not returned inside this
# bound is abandoned and logged as None, so a hung quote endpoint can neither
# delay nor suppress an order action or a terminal event. Pricing reads (the
# quotes a limit is computed from) are NOT routed through it — they are main's.
_DIAG_READ_TIMEOUT_SECONDS = 2.0


class CallRoller:
    """Daily credit-only covered-call rolling engine."""

    def __init__(self, alpaca_client: AlpacaClient, market_data: MarketDataManager,
                 config: Config,
                 risk_manager: RiskManager,
                 earnings_calendar: Optional[EarningsCalendarService] = None,
                 allow_bigquery_cost_basis: bool = True):
        """Initialize the call roller.

        Args:
            alpaca_client: Alpaca API client.
            market_data: Market data manager.
            config: Configuration instance.
            risk_manager: Validates the replacement leg.
            earnings_calendar: Earnings service for the FC-013 span gate on the
                replacement. Required whenever ``earnings.enabled`` — a missing
                service fails the roll CLOSED (``earnings_unknown``), never open.
            allow_bigquery_cost_basis: whether the cost-basis divergence
                cross-check may query BigQuery. A backtest passes False: the
                cross-check would otherwise read *production* trade history —
                against CURRENT_TIMESTAMP() — mixing real assignments into a
                simulated run. Mirrors ``CallSeller`` (FC-065).

        There is no state-manager parameter. FC-078 DD-6 deleted the roller's
        persistence dependency rather than repairing it: the only consumer was
        the debit tolerance's ``original_premium``, credit-only has no debit to
        tolerate, and ``STATE_STORAGE_BUCKET`` has been unset since project
        start so nothing was ever persisted to read back. Alpaca positions are
        the truth; ``reconcile_positions`` already rebuilds from them.
        """
        self.alpaca = alpaca_client
        self.market_data = market_data
        self.config = config
        self.risk_manager = risk_manager
        self.earnings_calendar = earnings_calendar
        # FC-065 Phase 2: the roll's strike floor is resolved through the same
        # shared resolver the scanner and the seller use — one floor
        # implementation, no fifth copy. The suite's hermeticity guard patches
        # ``_lookup_assignment_basis`` on the class, so this instance is covered
        # like the other two.
        self.cost_basis_resolver = CostBasisResolver(
            alpaca_client, config, allow_bigquery=allow_bigquery_cost_basis
        )
        # FC-120 PR-1: get_order_by_id reads issued by the latest
        # ``_poll_order_fill`` call. Log-only.
        self._last_poll_reads = 0
        # FC-120 PR-1 (ruling A): instrumentation work that must not sit
        # between an order action and the next one. Each entry does its own
        # bounded reads and emits its own row; they run after the NEXT order
        # placement, or after the position's terminal has been emitted.
        self._deferred: List[Callable[[], None]] = []
        # FC-120 PR-1 (ruling D): option symbol -> the terminal skip reason
        # this roller emitted for it this cycle. Read by the end-of-cycle quote
        # sampler only; never consulted by a decision.
        self.skip_reasons: Dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # Terminal events
    # ------------------------------------------------------------------ #
    def log_terminal_skip(self, option_symbol: str, underlying: str,
                          skip_reason: str, **fields: Any) -> None:
        """Emit the ``call_roll_skipped`` terminal event.

        Public because the cycle owns two skip reasons the roller cannot see
        from inside a single position's evaluation — ``cycle_budget_exhausted``
        and ``open_orders_unavailable`` — and the terminal-event contract is
        only worth anything if every path spells the event the same way.
        """
        _note_skip(self, option_symbol, skip_reason)
        log_trade_event(
            logger, event_type="call_roll_skipped",
            symbol=option_symbol, underlying=underlying,
            strategy="roll_call", success=False,
            skip_reason=skip_reason, gate_failed=skip_reason,
            **fields,
        )

    # ------------------------------------------------------------------ #
    # Eligibility
    # ------------------------------------------------------------------ #
    def should_roll(self, short_call_position: Dict[str, Any],
                    stock_position: Dict[str, Any],
                    current_stock_price: float,
                    open_order_symbols: Optional[Set[str]] = None
                    ) -> Tuple[bool, str]:
        """Check the pre-order eligibility gates for rolling a short call.

        FC-078 DD-2 deleted three gates and added one:

        - **DTE <= 1 is gone.** It existed to bound *debit* escalation near
          expiry; credit-only removes the hazard, and it is precisely what
          blinded the roller — a churned book's calls are 5-7 DTE on any given
          day (FC-066 cause 2). Self-timing is the credit sign, not the calendar.
        - **Max-rolls-per-position is gone.** It counted wheel-state rolls that
          were never persisted, so it always read 0 and never bound. The process
          terminates structurally instead: ``validate_roll`` requires strictly
          increasing strikes and every roll nets >= $0, so re-rolls are monotone
          and profitable.
        - **The fail-OPEN earnings blackout is gone**, subsumed by the fail-CLOSED
          span predicate applied to the *replacement* in evaluate_roll_opportunity.
        - **The open-order guard is new** (DD-4): ``/monitor`` places
          fire-and-forget DAY buy-to-close limits that outlive its 14:55 slot, so
          at 15:30 the roller can see a short call with a live BTC working
          against it. Rolling it could fill both buys — an unintended LONG call
          plus a sold replacement. We skip rather than cancel: the working order
          belongs to the profit-taker, which has precedence by design, and a
          cancel can lose the race to a fill anyway. Cost is one cycle.

        Returns:
            (should_roll, reason_code) — a bare reason code from the DD-5
            taxonomy, with the numbers carried as event fields rather than
            interpolated into the string.
        """
        option_symbol = short_call_position.get('symbol', '')
        parsed = parse_option_symbol(option_symbol)
        current_strike = parsed.get('strike_price', 0)

        if current_strike <= 0:
            return False, "invalid_strike"

        if open_order_symbols and option_symbol in open_order_symbols:
            return False, "open_order_conflict"

        ratio = current_stock_price / current_strike
        if ratio < self.config.rolling_itm_trigger_ratio:
            return False, "not_itm_enough"

        return True, "eligible"

    # ------------------------------------------------------------------ #
    # Evaluation
    # ------------------------------------------------------------------ #
    def evaluate_roll_opportunity(self, short_call_position: Dict[str, Any],
                                  stock_position: Dict[str, Any],
                                  open_order_symbols: Optional[Set[str]] = None
                                  ) -> Optional[Dict[str, Any]]:
        """Evaluate a short call and select the replacement to execute.

        Emits a terminal ``call_roll_skipped`` event on every failure path —
        including the three that used to ``return None`` silently (falsy or
        raising stock quote, zero stock price, zero BTC ask), which is FC-066
        checklist item 2 and the reason five Fridays of production told us
        nothing.

        Returns:
            Roll opportunity dict, or None if no roll (a terminal event fired).
        """
        option_symbol = short_call_position.get('symbol', '')
        parsed = parse_option_symbol(option_symbol)
        underlying = parsed.get('underlying', '')
        current_strike = parsed.get('strike_price', 0)
        old_expiry = coerce_expiry_date(parsed.get('expiration_date'))
        contracts = abs(int(float(short_call_position.get('qty', 0))))

        # --- Stock quote: two-sided, spread-sane, or no decision (DD-1/M-5) ---
        # get_stock_quote RAISES on failure — the as-built `if not quote` branch
        # was unreachable, which is why a data outage looked like "no roll today".
        try:
            quote = self.alpaca.get_stock_quote(underlying)
        except Exception as exc:
            self.log_terminal_skip(
                option_symbol, underlying, "stock_quote_unavailable",
                current_strike=current_strike, error=str(exc))
            return None
        if not quote:
            self.log_terminal_skip(
                option_symbol, underlying, "stock_quote_unavailable",
                current_strike=current_strike, error="empty quote")
            return None

        stock_bid = _as_float(quote.get('bid'))
        stock_ask = _as_float(quote.get('ask'))
        if stock_bid <= 0 or stock_ask <= 0 or stock_ask / stock_bid > _MAX_STOCK_SPREAD_RATIO:
            # Fail closed, both ways. The deleted "whichever side is positive"
            # fallback would answer a money question from half a quote.
            self.log_terminal_skip(
                option_symbol, underlying, "stock_quote_unusable",
                current_strike=current_strike,
                stock_bid=stock_bid, stock_ask=stock_ask,
                max_spread_ratio=_MAX_STOCK_SPREAD_RATIO)
            return None
        current_stock_price = (stock_bid + stock_ask) / 2

        # Earnings proximity, for log enrichment only. The gate itself is the
        # span predicate below.
        earnings_info: Dict[str, Any] = {}
        if self.earnings_calendar is not None:
            try:
                earnings_info = self.earnings_calendar.get_earnings_proximity(underlying) or {}
            except Exception:
                earnings_info = {}

        # --- Eligibility gates ---
        should, reason = self.should_roll(
            short_call_position, stock_position, current_stock_price,
            open_order_symbols)
        if not should:
            self.log_terminal_skip(
                option_symbol, underlying, reason,
                current_strike=current_strike,
                stock_price=current_stock_price,
                itm_ratio=(round(current_stock_price / current_strike, 4)
                           if current_strike > 0 else None),
                itm_trigger_ratio=self.config.rolling_itm_trigger_ratio,
                **earnings_info,
            )
            return None

        # --- Cost-basis floor, fail closed (FC-065 Phase 2) ---
        # Rolling is the one path that places orders without passing
        # execute_call_sale, so this floor is the only thing between a BTC/STO
        # pair and a strike below what the shares cost.
        shares = int(float(stock_position.get('qty', 0)))
        resolution = self.cost_basis_resolver.resolve_detailed(
            underlying, stock_position, shares)
        cost_basis_per_share = resolution['basis']

        if cost_basis_per_share <= 0:
            # resolve_detailed returns a zero basis for both verdicts that mean
            # "no usable floor", split into two events because they are
            # different failures: unresolved (nothing to floor against) vs
            # divergent (the broker gave a number and the assignment history
            # contradicts it — resolved and *vetoed*). Both are alert-wired
            # since FC-078 (FC-066 checklist item 1).
            cross_check = resolution.get('cross_check') or {}
            divergent = resolution.get('source') == SOURCE_DIVERGENT
            _note_skip(self, option_symbol, ("cost_basis_divergent" if divergent
                                             else "cost_basis_unresolved"))
            log_trade_event(
                logger,
                event_type=("call_roll_skipped_cost_basis_divergent" if divergent
                            else "call_roll_skipped_cost_basis_unresolved"),
                symbol=option_symbol, underlying=underlying,
                strategy="roll_call", success=False,
                skip_reason=("cost_basis_divergent" if divergent
                             else "cost_basis_unresolved"),
                current_strike=current_strike,
                stock_price=current_stock_price,
                shares=shares,
                cost_basis_source=resolution.get('source'),
                broker_basis=resolution.get('broker_basis'),
                expected_basis=cross_check.get('expected_basis'),
                basis_delta=cross_check.get('basis_delta'),
                tolerance=cross_check.get('tolerance'),
                cross_check_status=cross_check.get('status'),
                cross_check_reason=cross_check.get('reason'),
                **earnings_info,
            )
            return None

        # --- BTC quote and the pricing mode (DD-1/DD-2) ---
        btc_quote = self.alpaca.get_option_quote(option_symbol) or {}
        btc_ask = _as_float(btc_quote.get('ask'))
        btc_bid = _as_float(btc_quote.get('bid'))
        if btc_ask <= 0:
            self.log_terminal_skip(
                option_symbol, underlying, "btc_quote_unavailable",
                current_strike=current_strike,
                stock_price=current_stock_price, **earnings_info)
            return None

        extrinsic_per_share: Optional[float] = None
        imminent = False
        if btc_bid > 0 and btc_ask > 0:
            # A mid needs two sides. Without them there is no extrinsic estimate
            # worth acting on, so base pricing applies and the override does not
            # fire — no aggression on junk data.
            option_mid = (btc_bid + btc_ask) / 2
            intrinsic = max(0.0, current_stock_price - current_strike)
            extrinsic_per_share = max(0.0, option_mid - intrinsic)
            imminent = extrinsic_per_share <= self.config.rolling_imminence_extrinsic_threshold
        else:
            option_mid = None

        pricing_mode = 'imminence' if imminent else 'base'
        # Imminence mode crosses half the spread on both legs: when assignment is
        # imminent, bid/ask pricing double-counts the spread and the escape is
        # worth the fill risk. It relaxes NOTHING else — not the invariant, not
        # the floor, not the span gate.
        btc_limit = (round(option_mid + _IMMINENCE_PAD, 2) if imminent
                     else round(btc_ask, 2))

        # FC-120 PR-1 (T-MEDIUM-7): the quote this BTC limit was priced from
        # and the IEX stock quote, on the evaluation event and on every skip
        # that follows this point — no extra read; logging only.
        stock_quote_ts = _iso(quote.get('timestamp'))
        btc_quote_set = _evaluation_quote_set(
            btc_quote, btc_limit, underlying, current_strike,
            stock_bid, stock_ask, stock_quote_ts)

        # --- FC-013 span gate on the replacement, fail closed (DD-3) ---
        span_floor: Optional[date] = None
        if self.config.earnings_enabled:
            if self.earnings_calendar is None:
                # Never fail open on a missing service.
                self.log_terminal_skip(
                    option_symbol, underlying, "earnings_unknown",
                    current_strike=current_strike,
                    stock_price=current_stock_price,
                    earnings_status="no_calendar_service", **btc_quote_set)
                return None
            status, earnings_date = self.earnings_calendar.next_earnings_info(underlying)
            if status == EARNINGS_UNKNOWN:
                # A span test with no date cannot clear any candidate.
                self.log_terminal_skip(
                    option_symbol, underlying, "earnings_unknown",
                    current_strike=current_strike,
                    stock_price=current_stock_price,
                    earnings_status=status, **earnings_info, **btc_quote_set)
                return None
            if status == EARNINGS_KNOWN:
                span_floor = earnings_date

        # --- Candidate search ---
        if old_expiry is None:
            # Without the old expiry there is no horizon to bound the
            # replacement by. Fail closed rather than roll unbounded.
            self.log_terminal_skip(
                option_symbol, underlying, "invalid_expiry",
                current_strike=current_strike,
                stock_price=current_stock_price, **earnings_info,
                **btc_quote_set)
            return None
        max_expiry = old_expiry + timedelta(days=self.config.rolling_max_extension_days)

        min_strike = max(cost_basis_per_share, current_strike + 0.01)
        candidates = self.market_data.find_suitable_calls(
            underlying,
            min_strike_price=min_strike,
            exclude_expiry_on_or_after=span_floor,
            include_expiry_on_or_before=max_expiry,
            criteria_profile='roll',
        )
        if not candidates:
            self.log_terminal_skip(
                option_symbol, underlying, "no_suitable_replacement",
                current_strike=current_strike,
                stock_price=current_stock_price,
                min_strike=min_strike,
                max_expiry=max_expiry.isoformat(),
                # Named span_floor_date, not next_earnings_date: the latter is
                # already a get_earnings_proximity key and would collide.
                span_floor_date=(span_floor.isoformat() if span_floor else None),
                **earnings_info, **btc_quote_set)
            return None

        # --- Credit screen over the LEGAL set; execute maximum net credit ---
        min_credit_per_share = self.config.rolling_min_net_credit_per_contract / 100.0
        legal = self._legal_candidates(
            candidates, current_strike, cost_basis_per_share, max_expiry,
            btc_limit, min_credit_per_share, imminent)

        if not legal:
            self.log_terminal_skip(
                option_symbol, underlying, "no_credit_candidate",
                current_strike=current_strike,
                stock_price=current_stock_price,
                btc_limit=btc_limit,
                pricing_mode=pricing_mode,
                candidates_screened=len(candidates),
                min_net_credit_per_contract=self.config.rolling_min_net_credit_per_contract,
                **earnings_info, **btc_quote_set)
            return None

        primary = legal[0]

        opportunity = {
            'underlying': underlying,
            'old_option_symbol': option_symbol,
            'new_option_symbol': primary['new_option_symbol'],
            'old_strike': current_strike,
            'new_strike': primary['new_strike'],
            'old_expiry': old_expiry,
            'max_expiry': max_expiry,
            'contracts': contracts,
            'btc_limit': btc_limit,
            'stc_limit': primary['stc_limit'],
            'net_credit_per_contract': primary['net_credit_per_contract'],
            'pricing_mode': pricing_mode,
            'imminent': imminent,
            'extrinsic_per_share': extrinsic_per_share,
            'stock_price': current_stock_price,
            'cost_basis_per_share': cost_basis_per_share,
            'min_credit_per_share': min_credit_per_share,
            'earnings_info': earnings_info,
            'candidate': primary['candidate'],
            # FC-120 PR-1 instrumentation (logging only — no pricing input).
            # The IEX stock quote this evaluation decided on, as primitives,
            # and the raw BTC quote pair the limit was priced from.
            # The stamp is stored, not an age: every age is computed when the
            # row is written, on the same time base as ``quote_age_s``.
            'stock_bid': stock_bid,
            'stock_ask': stock_ask,
            'stock_quote_ts': stock_quote_ts,
            'btc_quote': btc_quote,
            # The ladder reuses THIS list and never re-queries the chain
            # (DD-1/M-8): a fresh find_suitable_calls at execute time could
            # admit candidates filtered under a different earnings-cache state
            # or drifted deltas, and an unfiltered order is a money bug. Price
            # freshness is obtained per-rung from the candidate's own quote.
            'fallback_candidates': legal,
        }

        log_trade_event(
            logger, event_type="call_roll_evaluated",
            symbol=option_symbol, underlying=underlying,
            strategy="roll_call", success=True,
            current_strike=current_strike,
            target_strike=primary['new_strike'],
            target_symbol=primary['new_option_symbol'],
            btc_limit=btc_limit, stc_limit=primary['stc_limit'],
            net_credit=primary['net_credit_per_contract'],
            net_credit_total=round(primary['net_credit_per_contract'] * contracts, 2),
            pricing_mode=pricing_mode,
            imminence=imminent,
            extrinsic_per_share=extrinsic_per_share,
            legal_candidates=len(legal),
            max_expiry=max_expiry.isoformat(),
            contracts=contracts,
            **earnings_info,
            **btc_quote_set,
        )
        return opportunity

    def _legal_candidates(self, candidates: List[Dict[str, Any]],
                          current_strike: float, cost_basis_per_share: float,
                          max_expiry: date, btc_limit: float,
                          min_credit_per_share: float,
                          imminent: bool) -> List[Dict[str, Any]]:
        """Screen every candidate, credit-ordered, most credit first.

        FC-078 DD-3: selection is **maximum net credit among legal candidates**,
        ties broken toward the higher strike — not first-past-the-post on
        ``return_score``. That score is annualised yield x OTM preference, an
        entry-shaped ordering; letting it pick the executed contract is how a
        book takes +$13 over +$248 on the same position. Every legal candidate
        already improves the strike strictly (``validate_roll``), so strike
        improvement is guaranteed and credit is the honest maximand. Paying
        certain credit for contingent strike value is a delta bet — the
        roll-up chasing this FC excludes.
        """
        legal: List[Dict[str, Any]] = []
        for candidate in candidates:
            valid, _reason = self.risk_manager.validate_roll(
                candidate, current_strike, cost_basis_per_share, max_expiry)
            if not valid:
                continue

            stc_limit = self._stc_limit_from_quote(
                _as_float(candidate.get('bid')), _as_float(candidate.get('ask')),
                imminent)
            if stc_limit is None:
                continue

            net = stc_limit - btc_limit
            if net + _CREDIT_EPSILON < min_credit_per_share:
                continue

            legal.append({
                'candidate': candidate,
                'new_option_symbol': candidate['symbol'],
                'new_strike': candidate.get('strike_price', 0),
                'stc_limit': stc_limit,
                'net_credit_per_share': net,
                'net_credit_per_contract': round(net * 100, 2),
            })

        legal.sort(key=lambda entry: (-entry['net_credit_per_share'],
                                      -entry['new_strike']))
        return legal

    @staticmethod
    def _stc_limit_from_quote(bid: float, ask: float,
                              imminent: bool) -> Optional[float]:
        """The sell-to-open limit for a pricing mode, or None if unquotable.

        Base mode sells at the bid — the conservative side, so the screened
        credit is the worst case rather than a hope. Imminence mode sells at
        mid - $0.05, which requires a two-sided quote; without one there is no
        mid, and the candidate is dropped rather than silently priced by another
        mode's rule.
        """
        if imminent:
            if bid <= 0 or ask <= 0:
                return None
            limit = round((bid + ask) / 2 - _IMMINENCE_PAD, 2)
        else:
            if bid <= 0:
                return None
            limit = round(bid, 2)
        return limit if limit > 0 else None

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #
    def execute_roll(self, opportunity: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a two-leg roll: buy-to-close, then sell-to-open.

        BTC is always first. STO-first would momentarily hold two short calls
        against 100 shares — a naked call Alpaca would either reject or margin.
        There is no acceptable STO-first sequence.

        Returns:
            Result dict with success status, order IDs, and fill details.

        FC-120 PR-1 (ruling A): instrumentation work queued by the body — a
        zero-fill leg's post-settle quote and its ``call_roll_leg_settled`` row —
        runs only after the body has emitted the position's terminal event and
        returned, or (STO rungs) right after the NEXT rung is placed. It never
        sits between a disposition and its terminal, or between two orders. If
        the body raises, queued work is dropped: an escaped exception is its
        own terminal (``call_roll_execution_error``) and nothing may precede it.
        """
        self._deferred = []
        result = self._execute_roll(opportunity)
        self._flush_deferred()
        return result

    def _execute_roll(self, opportunity: Dict[str, Any]) -> Dict[str, Any]:
        """The body of :meth:`execute_roll` — order actions and terminals,
        byte-for-byte main's sequence."""
        underlying = opportunity['underlying']
        old_symbol = opportunity['old_option_symbol']
        new_symbol = opportunity['new_option_symbol']
        contracts = opportunity['contracts']
        btc_limit = opportunity['btc_limit']
        earnings_info = opportunity.get('earnings_info', {})
        min_credit = opportunity['min_credit_per_share']

        _start_roll_ctx(opportunity)

        if self.config.roller_dry_run:
            # FC-120 PR-1: the dry run carries the BTC placement set and the
            # STO candidate's (chain-row) set as ``stc_*``, so PR-2's dry-run
            # verification can be read off this one event. No extra read.
            payload = self._placement_fields(
                opportunity, leg='btc', quote=opportunity.get('btc_quote'),
                limit=btc_limit, strike=opportunity.get('old_strike'))
            payload.update(self._dry_run_stc_fields(opportunity))
            payload.update(
                would_be_btc_symbol=old_symbol,
                would_be_btc_limit=btc_limit,
                would_be_stc_symbol=new_symbol,
                would_be_stc_limit=opportunity['stc_limit'],
                net_credit=opportunity['net_credit_per_contract'],
                pricing_mode=opportunity['pricing_mode'],
                contracts=contracts,
            )
            payload.update(earnings_info)
            log_trade_event(
                logger, event_type="call_roll_dry_run",
                symbol=old_symbol, underlying=underlying,
                strategy="roll_call", success=True, **payload)
            return {'success': False, 'reason': 'dry_run',
                    'underlying': underlying}

        # --- Pre-BTC invariant re-check (DD-1) ---
        # Evaluation quotes are minutes old by order time. Re-pricing the STO
        # leg here shrinks the staleness window to the BTC poll itself, and
        # losing the race HERE costs nothing: no order exists yet.
        fresh = self.alpaca.get_option_quote(new_symbol) or {}
        fresh_stc_limit = self._stc_limit_from_quote(
            _as_float(fresh.get('bid')), _as_float(fresh.get('ask')),
            opportunity['imminent'])
        if fresh_stc_limit is None or \
                (fresh_stc_limit - btc_limit) + _CREDIT_EPSILON < min_credit:
            self.log_terminal_skip(
                old_symbol, underlying, "credit_gone_at_execution",
                current_strike=opportunity['old_strike'],
                target_strike=opportunity['new_strike'],
                btc_limit=btc_limit,
                evaluated_stc_limit=opportunity['stc_limit'],
                recheck_stc_limit=fresh_stc_limit,
                pricing_mode=opportunity['pricing_mode'],
                **earnings_info, **self._opportunity_quote_set(opportunity))
            return {'success': False, 'reason': 'credit_gone_at_execution',
                    'underlying': underlying}
        opportunity['stc_limit'] = fresh_stc_limit
        if opportunity['fallback_candidates']:
            opportunity['fallback_candidates'][0]['stc_limit'] = fresh_stc_limit
        # FC-120 PR-1: rung 1 is priced from ``fresh``; stash it for rung 1's
        # rows (log only). NO read here — ruling A: nothing sits between this
        # re-check and the BTC placement; rung 1's re-read follows ITS placement.
        opportunity['stc_quote_rung1'] = fresh

        # === LEG 1: Buy-to-close ===
        # FC-120 PR-1: the quote this limit was priced from (DD-2), computed
        # at placement so ``quote_age_s`` is the age when the order went out.
        btc_quote = opportunity.get('btc_quote')
        btc_fields = self._placement_fields(
            opportunity, leg='btc', quote=btc_quote, limit=btc_limit,
            strike=opportunity['old_strike'])
        placed_payload = dict(btc_fields)
        placed_payload.update(
            current_strike=opportunity['old_strike'],
            contracts=contracts, limit_price=btc_limit,
            pricing_mode=opportunity['pricing_mode'])
        log_trade_event(
            logger, event_type="call_roll_btc_placed",
            symbol=old_symbol, underlying=underlying,
            strategy="roll_call", success=True, **placed_payload)

        btc_t0 = time.monotonic()
        btc_result = self.alpaca.place_option_order(
            symbol=old_symbol, qty=contracts,
            side='buy', order_type='limit', limit_price=btc_limit)

        def btc_settled(disposition: str, order_id: Optional[str], **kw: Any) -> None:
            self._log_leg_settled(
                symbol=old_symbol, underlying=underlying, order_id=order_id,
                disposition=disposition, placement=btc_fields, limit=btc_limit,
                requested_qty=contracts, **kw)

        if not btc_result or not btc_result.get('success', False):
            error_msg = (btc_result.get('error_message', 'BTC order rejected')
                         if btc_result else 'No result')
            log_error_event(
                logger, error_type="call_roll_btc_rejected",
                error_message=error_msg, component="call_roller",
                recoverable=True, symbol=old_symbol, underlying=underlying,
                limit_price=btc_limit, contracts=contracts,
            )
            btc_settled('rejected', (btc_result or {}).get('order_id'),
                        elapsed=round(time.monotonic() - btc_t0, 3))
            return {'success': False, 'reason': 'btc_rejected',
                    'error': error_msg, 'underlying': underlying}

        btc_order_id = btc_result.get('order_id', '')
        _roll_ctx(opportunity)['btc_order_id'] = btc_order_id
        # FC-120 PR-1 (rulings A/B): the re-read is taken only now that the
        # order is out, bounded, and rides on this leg's later rows.
        btc_fields.update(_reread_fields(
            btc_quote, self._diag_option_quote(old_symbol), 'buy'))

        # --- Poll, then cancel-and-VERIFY on timeout ---
        # "Cancel failed because it filled" is a fill, not an error. The
        # as-built code returned on timeout with the DAY order still live and
        # reported `btc_unfilled` without ever looking again — a fiction that
        # would have been logged while contracts were being bought.
        btc_order = self._poll_order_fill(btc_order_id)
        # FC-120 T7: snapshot BEFORE the settle — _cancel_and_settle re-enters
        # _poll_order_fill and would overwrite the count.
        btc_polls = self._last_poll_reads
        btc_settle_polls: Optional[int] = None
        timed_out = btc_order is None
        if timed_out:
            btc_order = self._cancel_and_settle(btc_order_id)
            btc_settle_polls = self._last_poll_reads
        btc_elapsed = round(time.monotonic() - btc_t0, 3)
        if timed_out:
            if btc_order is None:
                btc_settled('unknown', btc_order_id, elapsed=btc_elapsed,
                            polls=btc_polls, settle_polls=btc_settle_polls)
                # The cancel never settled. We do NOT know whether contracts
                # were bought, so we must not sell a call against a short
                # position whose size is unknown. Stop, and page.
                return self._unknown_disposition(
                    leg='btc', order_id=btc_order_id, symbol=old_symbol,
                    underlying=underlying, earnings_info=earnings_info,
                    detail=("BTC cancel did not settle to a terminal status "
                            "within the bound; the order may still be working"))

        btc_filled_qty = int(_as_float(btc_order.get('filled_qty')))
        if btc_filled_qty <= 0:
            btc_status = btc_order.get('status')
            if btc_status == 'rejected':
                # L-2: a broker rejection after placement is a REJECTION, not a
                # timeout-cancel. The event name has to say which happened —
                # "canceled at timeout" and "the broker refused it" call for
                # different investigations.
                log_error_event(
                    logger, error_type="call_roll_btc_rejected",
                    error_message=f"BTC order {btc_order_id} rejected after placement",
                    component="call_roller", recoverable=True,
                    symbol=old_symbol, underlying=underlying,
                    order_id=btc_order_id, limit_price=btc_limit,
                    contracts=contracts, rejected_after_placement=True,
                )
                btc_settled('rejected', btc_order_id, order=btc_order,
                            elapsed=btc_elapsed, polls=btc_polls,
                            settle_polls=btc_settle_polls)
                return {'success': False, 'reason': 'btc_rejected',
                        'order_id': btc_order_id, 'underlying': underlying}
            disposition = _zero_fill_disposition(btc_status, timed_out)
            # FC-120 PR-1 (ruling A): the terminal goes out FIRST, with the
            # fields already in hand. The post-settle quote is read only after
            # it, and its fields ride on the later leg_settled row alone.
            timeout_payload = _safe_merge(btc_fields, dict(
                order_id=btc_order_id,
                order_status=_str_or_none(btc_status),
                disposition=disposition,
                limit_price=btc_limit, contracts=contracts,
                leg_elapsed_s=btc_elapsed,
                order_submitted_at=_iso(btc_order.get('submitted_at')),
                polls=btc_polls, settle_polls=btc_settle_polls,
            ))
            timeout_payload.update(earnings_info)
            log_trade_event(
                logger, event_type="call_roll_btc_timeout_canceled",
                symbol=old_symbol, underlying=underlying,
                strategy="roll_call", success=False, **timeout_payload)

            def settle_row() -> None:
                cancel_fields = self._post_settle_quote(
                    old_symbol, underlying, btc_quote, 'buy')
                btc_settled(disposition, btc_order_id, order=btc_order,
                            cancel=cancel_fields, elapsed=btc_elapsed,
                            polls=btc_polls, settle_polls=btc_settle_polls)
            self._deferred.append(settle_row)
            return {'success': False, 'reason': 'btc_timeout_canceled',
                    'order_id': btc_order_id, 'underlying': underlying}

        btc_filled_price = _as_float(btc_order.get('filled_avg_price')) or btc_limit
        # FC-120 PR-1 (T-LOW-8): was the price the broker's, or the limit
        # standing in for an absent ``filled_avg_price``? Log-only.
        btc_price_source = _fill_price_source(btc_order)

        filled_payload = dict(btc_fields)
        filled_payload.update(self._fill_fields(
            limit=btc_limit, filled_price=btc_filled_price, side='buy',
            order=btc_order, elapsed=btc_elapsed, polls=btc_polls,
            settle_polls=btc_settle_polls, price_source=btc_price_source))
        filled_payload.update(
            order_id=btc_order_id,
            filled_qty=btc_filled_qty,
            filled_price=btc_filled_price,
            requested_qty=contracts,
        )
        log_trade_event(
            logger, event_type="call_roll_btc_filled",
            symbol=old_symbol, underlying=underlying,
            strategy="roll_call", success=True, **filled_payload)
        btc_settled('filled' if btc_filled_qty >= contracts else 'partial',
                    btc_order_id, filled_qty=btc_filled_qty,
                    filled_price=btc_filled_price, order=btc_order,
                    elapsed=btc_elapsed, polls=btc_polls,
                    settle_polls=btc_settle_polls,
                    price_source=btc_price_source)

        if btc_filled_qty < contracts:
            # Partial-fill truth (DD-1). The remainder is dead — either our
            # cancel above killed it or the order reached a terminal status —
            # and the position keeps `contracts - filled` of the OLD call, still
            # covered, still evaluated tomorrow. The STO leg must be sized to
            # what actually closed; the as-built code passed the REQUESTED qty,
            # which would sell a call against shares still pledged to the
            # unclosed remainder.
            #
            # No cancel is needed here: either the timeout path above already
            # canceled, or the poll returned a TERMINAL status, which means
            # nothing of this order is still working.
            log_trade_event(
                logger, event_type="call_roll_partial_fill",
                symbol=old_symbol, underlying=underlying,
                strategy="roll_call", success=True,
                leg="btc",
                requested_qty=contracts,
                filled_qty=btc_filled_qty,
                unfilled_qty=contracts - btc_filled_qty,
            )

        # === LEG 2: Sell-to-open, sized to what actually closed ===
        stc_result = self._attempt_stc(opportunity, btc_filled_qty, btc_filled_price)

        if stc_result and stc_result.get('unknown_disposition'):
            return self._unknown_disposition(
                leg='stc', order_id=stc_result.get('order_id', ''),
                symbol=stc_result.get('symbol', new_symbol),
                underlying=underlying, earnings_info=earnings_info,
                detail=("STO cancel did not settle to a terminal status within "
                        "the bound; the order may still be working, so no "
                        "further sell was placed"),
                btc_order_id=btc_order_id, btc_filled_qty=btc_filled_qty)

        if stc_result and stc_result.get('success'):
            stc_filled_qty = stc_result['filled_qty']
            stc_filled_price = stc_result['filled_price']

            # Per-replaced-quantity accounting (FC-078 review, execution H-2 =
            # trader M-1). The shipped version computed
            # ``stc_price*stc_qty - btc_price*btc_qty`` across MIXED quantities,
            # so a 2-contract BTC against a 1-contract STO reported
            # ``call_roll_completed`` with net_credit = -$590 — a blended DEBIT
            # labelled a credit — while 100 shares sat uncovered with no
            # naked-exposure-class event. The credit invariant is a per-contract
            # guarantee; only a per-contract number may be called a credit.
            replaced = min(stc_filled_qty, btc_filled_qty)
            uncovered = btc_filled_qty - replaced
            net_credit = (stc_filled_price - btc_filled_price) * replaced * 100
            btc_cash_paid = btc_filled_price * btc_filled_qty * 100
            stc_cash_received = stc_filled_price * stc_filled_qty * 100

            if uncovered > 0:
                log_trade_event(
                    logger, event_type="call_roll_partial_fill",
                    symbol=stc_result['symbol'], underlying=underlying,
                    strategy="roll_call", success=True,
                    leg="stc",
                    requested_qty=btc_filled_qty,
                    filled_qty=stc_filled_qty,
                    unfilled_qty=uncovered,
                )
                # The remainder is exactly the naked-exposure case, at a smaller
                # quantity — plan §1 already said so ("the uncovered remainder
                # falls through to the naked-exposure terminal if no later rung
                # covers it"); the code did not. One terminal, alert-wired,
                # reporting both legs explicitly rather than a blended figure.
                log_error_event(
                    logger, error_type="call_roll_partial_naked_exposure",
                    error_message=(
                        f"Rolled {replaced} of {btc_filled_qty} contracts — "
                        f"{uncovered} contract(s) worth of shares "
                        f"({uncovered * 100}) are UNCOVERED (no naked position); "
                        f"next /scan -> /run re-covers through the entry path "
                        f"with all entry gates applied"),
                    component="call_roller", recoverable=False,
                    symbol=stc_result['symbol'], underlying=underlying,
                    old_option_symbol=old_symbol,
                    contracts_replaced=replaced,
                    contracts_uncovered=uncovered,
                    btc_filled_qty=btc_filled_qty,
                    btc_filled_price=btc_filled_price,
                    btc_cash_paid=round(btc_cash_paid, 2),
                    stc_filled_qty=stc_filled_qty,
                    stc_filled_price=stc_filled_price,
                    stc_cash_received=round(stc_cash_received, 2),
                    net_credit_on_replaced=round(net_credit, 2),
                    btc_order_id=btc_order_id,
                    stc_order_id=stc_result['order_id'],
                )
                return {
                    'success': False,
                    'reason': 'partial_naked_exposure',
                    'underlying': underlying,
                    'contracts_replaced': replaced,
                    'contracts_uncovered': uncovered,
                    'net_credit_on_replaced': round(net_credit, 2),
                    'btc_order_id': btc_order_id,
                    'stc_order_id': stc_result['order_id'],
                }

            log_position_update(
                logger, event_type="call_roll_completed",
                symbol=underlying,
                position_status="rolled",
                old_strike=opportunity['old_strike'],
                new_strike=stc_result.get('new_strike', opportunity['new_strike']),
                old_option_symbol=old_symbol,
                new_option_symbol=stc_result['symbol'],
                net_credit=round(net_credit, 2),
                contracts=stc_filled_qty,
                pricing_mode=opportunity['pricing_mode'],
                btc_filled_price=btc_filled_price,
                stc_filled_price=stc_filled_price,
                btc_order_id=btc_order_id,
                stc_order_id=stc_result['order_id'],
                **earnings_info,
            )

            return {
                'success': True,
                'underlying': underlying,
                'old_strike': opportunity['old_strike'],
                'new_strike': stc_result.get('new_strike', opportunity['new_strike']),
                'contracts': stc_filled_qty,
                'net_credit': round(net_credit, 2),
                'btc_order_id': btc_order_id,
                'stc_order_id': stc_result['order_id'],
            }

        # STO ladder exhausted after a BTC fill. The shares are uncovered but
        # LONG-ONLY — there is no naked position — and the residual is not "a
        # day's premium leak": worst case is the full BTC debit paid in cash
        # with nothing sold against it, re-covered later through the entry path
        # at entry-band premiums, adversely selected. Near a report the FC-013
        # span gate can legitimately leave the shares uncovered through the
        # event. That is correct by doctrine and a real terminal outcome, which
        # is why this event is alert-wired rather than absorbed.
        log_error_event(
            logger, error_type="call_roll_naked_exposure",
            error_message=("STO ladder exhausted after BTC filled — shares "
                           "uncovered (no naked position); next /scan -> /run "
                           "re-covers through the entry path with all entry "
                           "gates applied"),
            component="call_roller", recoverable=False,
            symbol=new_symbol, underlying=underlying,
            btc_order_id=btc_order_id,
            btc_filled_qty=btc_filled_qty,
            btc_filled_price=btc_filled_price,
        )
        return {
            'success': False,
            'reason': 'stc_failed_naked_exposure',
            'btc_order_id': btc_order_id,
            'btc_filled_qty': btc_filled_qty,
            'underlying': underlying,
        }

    def _unknown_disposition(self, *, leg: str, order_id: str, symbol: str,
                             underlying: str, earnings_info: Dict[str, Any],
                             detail: str, **fields: Any) -> Dict[str, Any]:
        """The fail-safe terminal: an order whose fate we could not establish.

        This exists because the honest answer to "did that cancel work?" is
        sometimes "we don't know", and every other answer the code could give is
        a guess about live contracts. It is alert-wired precisely because it
        needs a human to look at the account — the bot deliberately stops
        touching the position rather than acting on a guess.
        """
        log_error_event(
            logger, error_type="call_roll_unknown_disposition",
            error_message=(
                f"{leg.upper()} order {order_id} disposition UNKNOWN — {detail}. "
                f"No further orders placed for this position this cycle; "
                f"check the account before the next cycle."),
            component="call_roller", recoverable=False,
            symbol=symbol, underlying=underlying, leg=leg, order_id=order_id,
            **fields,
        )
        return {'success': False, 'reason': f'{leg}_disposition_unknown',
                'order_id': order_id, 'underlying': underlying}

    # ------------------------------------------------------------------ #
    # The STO ladder
    # ------------------------------------------------------------------ #
    def _attempt_stc(self, opportunity: Dict[str, Any], qty: int,
                     btc_filled_price: float) -> Optional[Dict[str, Any]]:
        """Sell-to-open with a bounded retry ladder.

        **At most one STO order is live at any instant** — an invariant, not an
        aspiration. As-built, a rung that timed out left its DAY limit working
        and the ladder placed the next sell on top of it: two live sells against
        one covered lot, and a late fill on rung 1 while rung 2 works is a
        genuine **naked short call**. Every rung transition here is
        cancel-then-SETTLE, and a cancel that fails because the order filled is
        reported as that rung *succeeding*.

        The invariant needs the settle, not just the cancel (review H-1): an
        Alpaca cancel is queued, so a rung sitting in ``pending_cancel`` is
        still working. If a rung will not settle, the ladder **stops** — placing
        the next sell against an unresolved one is exactly the naked-call window
        this method exists to close.
        """
        underlying = opportunity['underlying']
        # (order_id, symbol, strike, limit, leg_ctx) — leg_ctx is FC-120 PR-1's
        # log-only context (placement fields, clock, poll count).
        live: Optional[Tuple[str, str, float, float, Dict[str, Any]]] = None
        rung1_reread: Dict[str, Any] = {k: None for k in _REREAD_KEYS}

        for symbol, limit, new_strike, rung_quote in self._rungs(
                opportunity, btc_filled_price):
            if live is not None:
                pending = live
                live = None
                resolved, settled = self._settle_live_rung(pending, underlying)
                if resolved:
                    return resolved
                if not settled:
                    return {'success': False, 'unknown_disposition': True,
                            'order_id': pending[0], 'symbol': pending[1]}

            rung = rung_quote.get('rung')
            fields = self._placement_fields(
                opportunity, leg='stc', quote=rung_quote.get('quote'),
                limit=limit, strike=new_strike,
                rung=rung, reused_from_rung=rung_quote.get('reused_from_rung'),
                chain_stamp=(rung_quote.get('chain_stamp')
                             if rung == 1 else None))
            ctx: Dict[str, Any] = {'placement': fields, 'limit': limit,
                                   'quote': rung_quote.get('quote'),
                                   'requested_qty': qty, 'rung': rung,
                                   't0': time.monotonic(), 'polls': None}
            if rung == 2:
                # ruling E: rung 2 is reached only after rung 1 missed, so a
                # rung-2 fill measures rung 1's miss offset. Log-only.
                ctx['prior_rung_limit'] = opportunity.get('stc_limit')
            order_id = self._place_stc(symbol, underlying, qty, limit,
                                       rung=rung, quote_fields=fields,
                                       pricing_mode=opportunity.get('pricing_mode'))
            if order_id is None:
                self._log_stc_settled(ctx, symbol, underlying, None, 'rejected')
                continue

            # FC-120 PR-1 (ruling A): only now that this rung is OUT — its own
            # re-read (rung 2 reuses rung 1's quote and re-read), then the
            # previous rung's post-settle work. Both bounded (ruling B).
            if rung_quote.get('reused_from_rung'):
                fields.update(rung1_reread)
            else:
                fields.update(_reread_fields(
                    rung_quote.get('quote'), self._diag_option_quote(symbol),
                    'sell'))
                if rung == 1:
                    rung1_reread = {k: fields.get(k) for k in _REREAD_KEYS}
            self._flush_deferred()

            order = self._poll_order_fill(order_id)
            ctx['polls'] = self._last_poll_reads
            if order is None:
                # Timed out with the order still working — carry it forward so
                # the NEXT rung cancels-and-verifies before placing anything.
                live = (order_id, symbol, new_strike, limit, ctx)
                continue
            ctx['elapsed'] = round(time.monotonic() - ctx['t0'], 3)

            filled_qty = int(_as_float(order.get('filled_qty')))
            if filled_qty > 0:
                return self._stc_success(order_id, symbol, underlying, new_strike,
                                         filled_qty, order, limit, ctx=ctx)
            # Terminal with zero fill (rejected / expired / canceled): nothing
            # is live, so the next rung may be placed directly.
            status = order.get('status')
            # FC-120 PR-1 (ruling A): the pre-existing event first, fields in
            # hand; the post-settle read waits for the next placement (or the
            # terminal) and its fields ride on the leg_settled row only.
            payload = _safe_merge(fields, dict(
                order_status=_str_or_none(status), limit_price=limit,
                leg_elapsed_s=ctx['elapsed'], polls=ctx['polls'],
                order_submitted_at=_iso(order.get('submitted_at'))))
            payload.pop('symbol', None)
            payload.pop('underlying', None)
            payload.pop('order_id', None)
            log_error_event(
                logger, error_type="call_roll_stc_unfilled",
                error_message=f"STO order {order_id} status={status}",
                component="call_roller", recoverable=True,
                symbol=symbol, underlying=underlying, order_id=order_id,
                **payload,
            )
            self._defer_stc_settled(
                ctx, symbol, underlying, order_id,
                _zero_fill_disposition(status, False), order)

        if live is not None:
            resolved, settled = self._settle_live_rung(live, underlying)
            if resolved:
                return resolved
            if not settled:
                return {'success': False, 'unknown_disposition': True,
                        'order_id': live[0], 'symbol': live[1]}

        return None

    def _rungs(self, opportunity: Dict[str, Any],
               btc_filled_price: float
               ) -> Iterator[Tuple[str, float, float, Dict[str, Any]]]:
        """Yield ``(symbol, limit, strike, rung_quote)`` per ladder rung, priced
        when reached.

        ``rung_quote`` (FC-120 PR-1, log-only) names the rung by its LADDER
        POSITION — 1 primary, 2 floor, 3+ the fallback list in order, so a
        number is never reused for a different kind of rung — and carries the
        quote that rung was priced from. Rung 2 has no quote of its own; it
        carries rung 1's with ``reused_from_rung=1``. Rung 1 carries the chain
        row's stamp. Nothing in it feeds a limit, and the generator issues no
        read beyond main's pricing quote for rungs 3+.

        Rung 1 — the primary candidate at the pre-BTC re-checked limit.
        Rung 2 — the primary at the invariant *minimum* price
                 (``btc_filled_price + min_credit``), used only when that is
                 BELOW rung 1's price, i.e. only when the BTC filled better than
                 its limit. Never below the minimum: that is the invariant.
        Rungs 3+ — up to ``fallback_strike_attempts`` further candidates from the
                 stored legal list, each re-quoted, re-validated through
                 ``validate_roll``, and re-tested against the invariant computed
                 from the **actual BTC fill price**. Candidates are never
                 re-queried from the chain (M-8): the stored list is the one that
                 passed the span gate, the profile and the floor.
        """
        min_credit = opportunity['min_credit_per_share']
        floor_price = round(btc_filled_price + min_credit, 2)
        primary_symbol = opportunity['new_option_symbol']
        primary_limit = opportunity['stc_limit']
        rung1_quote = opportunity.get('stc_quote_rung1')
        candidate_row = opportunity.get('candidate')

        yield (primary_symbol, primary_limit, opportunity['new_strike'],
               {'rung': 1, 'quote': rung1_quote,
                'chain_stamp': (candidate_row.get('quote_timestamp')
                                if isinstance(candidate_row, dict) else None)})

        if 0 < floor_price < primary_limit:
            yield (primary_symbol, floor_price, opportunity['new_strike'],
                   {'rung': 2, 'quote': rung1_quote, 'reused_from_rung': 1})

        attempts = self.config.rolling_fallback_strike_attempts
        for position, entry in enumerate(
                opportunity['fallback_candidates'][1:1 + attempts], start=3):
            symbol = entry['new_option_symbol']
            quote = self.alpaca.get_option_quote(symbol) or {}
            limit = self._stc_limit_from_quote(
                _as_float(quote.get('bid')), _as_float(quote.get('ask')),
                opportunity['imminent'])
            if limit is None:
                continue

            valid, _reason = self.risk_manager.validate_roll(
                entry['candidate'], opportunity['old_strike'],
                opportunity['cost_basis_per_share'], opportunity['max_expiry'])
            if not valid:
                continue

            # The invariant against what the BTC ACTUALLY cost, not its limit.
            if (limit - btc_filled_price) + _CREDIT_EPSILON < min_credit:
                continue

            # FC-120 PR-1: the rung's own quote rides along for the log; its
            # re-read is taken after the rung is placed (ruling A).
            yield (symbol, limit, entry['new_strike'],
                   {'rung': position, 'quote': quote})

    def _settle_live_rung(self, live: Tuple[str, str, float, float],
                          underlying: str
                          ) -> Tuple[Optional[Dict[str, Any]], bool]:
        """Cancel a working STO rung and wait for the broker to settle it.

        Returns ``(result, settled)``:

        - ``(success_dict, True)`` — the cancel lost the race to a fill, so that
          rung SUCCEEDED. A partial fill counts as that rung's result; the
          uncovered remainder is reported by the caller.
        - ``(None, True)`` — settled terminal with nothing filled. Safe to place
          the next rung.
        - ``(None, False)`` — **did not settle.** The order may still be
          working, so the ladder must stop: placing another sell here is the
          two-live-sells window in its purest form.
        """
        order_id, symbol, new_strike, limit_price = live[:4]
        ctx: Optional[Dict[str, Any]] = live[4] if len(live) > 4 else None
        order = self._cancel_and_settle(order_id)
        if ctx is not None:
            ctx['settle_polls'] = self._last_poll_reads
            ctx['elapsed'] = round(time.monotonic() - ctx['t0'], 3)
        if order is None:
            log_error_event(
                logger, error_type="call_roll_stc_disposition_unknown",
                error_message=(
                    f"STO order {order_id} did not settle after cancel; "
                    f"ladder stopped rather than placing another sell"),
                component="call_roller", recoverable=False,
                symbol=symbol, underlying=underlying, order_id=order_id,
            )
            if ctx is not None:
                self._log_stc_settled(ctx, symbol, underlying, order_id, 'unknown')
            return None, False
        filled_qty = int(_as_float(order.get('filled_qty')))
        if filled_qty > 0:
            return self._stc_success(order_id, symbol, underlying, new_strike,
                                     filled_qty, order, limit_price,
                                     ctx=ctx), True
        if ctx is not None:
            # FC-120 PR-1: this zero-fill path used to log nothing for the rung.
            # Ruling A: the rung's event now, with the fields in hand; the
            # post-settle read is deferred past the NEXT rung's placement and
            # rides on the leg_settled row alone.
            disposition = _zero_fill_disposition(order.get('status'), True)
            payload = _safe_merge(ctx['placement'], dict(
                order_id=order_id, order_status=_str_or_none(order.get('status')),
                disposition=disposition, limit_price=limit_price,
                contracts=ctx['requested_qty'], leg_elapsed_s=ctx['elapsed'],
                order_submitted_at=_iso(order.get('submitted_at')),
                polls=ctx['polls'], settle_polls=ctx.get('settle_polls')))
            log_trade_event(
                logger, event_type="call_roll_stc_timeout_canceled",
                symbol=symbol, underlying=underlying,
                strategy="roll_call", success=False, **payload)
            self._defer_stc_settled(ctx, symbol, underlying, order_id,
                                    disposition, order)
        return None, True

    def _defer_stc_settled(self, ctx: Dict[str, Any], symbol: str,
                           underlying: str, order_id: Optional[str],
                           disposition: str, order: Dict[str, Any]) -> None:
        """Queue a zero-fill rung's post-settle quote + leg_settled row.

        Ruling A: it runs right after the NEXT rung is placed, or after the
        position's terminal when the ladder ends — never between this rung's
        settle and the next order action."""
        def settle_row() -> None:
            cancel = self._post_settle_quote(symbol, underlying, ctx.get('quote'),
                                             'sell')
            self._log_stc_settled(ctx, symbol, underlying, order_id,
                                  disposition, order=order, cancel=cancel)
        self._deferred.append(settle_row)

    def _log_stc_settled(self, ctx: Dict[str, Any], symbol: str,
                         underlying: str, order_id: Optional[str],
                         disposition: str, *,
                         order: Optional[Dict[str, Any]] = None,
                         cancel: Optional[Dict[str, Any]] = None,
                         filled_qty: int = 0, filled_price: Any = None) -> None:
        """The STO rung's ``call_roll_leg_settled`` row, from its leg context."""
        elapsed = ctx.get('elapsed')
        if elapsed is None and ctx.get('t0') is not None:
            elapsed = round(time.monotonic() - ctx['t0'], 3)
        self._log_leg_settled(
            symbol=symbol, underlying=underlying, order_id=order_id,
            disposition=disposition, placement=ctx['placement'],
            limit=ctx['limit'], requested_qty=ctx['requested_qty'],
            filled_qty=filled_qty, filled_price=filled_price, order=order,
            cancel=cancel, elapsed=elapsed, polls=ctx.get('polls'),
            settle_polls=ctx.get('settle_polls'),
            price_source=ctx.get('price_source'), extra=ctx.get('extra'))

    def _place_stc(self, symbol: str, underlying: str, contracts: int,
                   limit_price: float, *, rung: Optional[int] = None,
                   quote_fields: Optional[Dict[str, Any]] = None,
                   pricing_mode: Optional[str] = None) -> Optional[str]:
        """Place one STO rung. Returns the order id, or None if it was refused."""
        payload = dict(quote_fields or {})
        payload.update(contracts=contracts, limit_price=limit_price,
                       leg='stc', rung=rung, pricing_mode=pricing_mode)
        log_trade_event(
            logger, event_type="call_roll_stc_placed",
            symbol=symbol, underlying=underlying,
            strategy="roll_call", success=True, **payload)

        result = self.alpaca.place_option_order(
            symbol=symbol, qty=contracts,
            side='sell', order_type='limit', limit_price=limit_price)

        if not result or not result.get('success', False):
            log_error_event(
                logger, error_type="call_roll_stc_rejected",
                error_message=(result.get('error_message', 'STO order rejected')
                               if result else 'No result'),
                component="call_roller", recoverable=True,
                symbol=symbol, underlying=underlying, limit_price=limit_price,
            )
            return None
        return result.get('order_id', '')

    def _stc_success(self, order_id: str, symbol: str, underlying: str,
                     new_strike: float, filled_qty: int, order: Dict[str, Any],
                     limit_price: Optional[float], *,
                     ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        filled_price = _as_float(order.get('filled_avg_price'))
        if filled_price <= 0 and limit_price is not None:
            filled_price = limit_price
        payload: Dict[str, Any] = {}
        if ctx is not None:
            # FC-120 PR-1: the placement set and the fill-quality fields.
            payload = self._stc_fill_payload(ctx, order, limit_price,
                                             filled_price)
        payload.update(order_id=order_id, filled_qty=filled_qty,
                       filled_price=filled_price)
        log_trade_event(
            logger, event_type="call_roll_stc_filled",
            symbol=symbol, underlying=underlying,
            strategy="roll_call", success=True, **payload)
        if ctx is not None:
            self._log_stc_settled(
                ctx, symbol, underlying, order_id,
                'filled' if filled_qty >= ctx['requested_qty'] else 'partial',
                order=order, filled_qty=filled_qty, filled_price=filled_price)
        return {
            'success': True,
            'order_id': order_id,
            'filled_qty': filled_qty,
            'filled_price': filled_price,
            'symbol': symbol,
            'new_strike': new_strike,
        }

    def _stc_fill_payload(self, ctx: Dict[str, Any], order: Dict[str, Any],
                          limit_price: Optional[float],
                          filled_price: float) -> Dict[str, Any]:
        """The STO fill event's FC-120 fields. Total (ruling C): any failure
        is an empty dict plus one breadcrumb, never a raise."""
        try:
            payload: Dict[str, Any] = {}
            source = _fill_price_source(order)
            ctx['price_source'] = source
            ctx['extra'] = {'prior_rung_miss_offset': (
                _safe_diff(ctx.get('prior_rung_limit'), filled_price)
                if source == 'broker' and ctx.get('prior_rung_limit') is not None
                else None)} if ctx.get('rung') == 2 else {}
            payload.update(ctx['placement'])
            payload.update(self._fill_fields(
                limit=limit_price, filled_price=filled_price, side='sell',
                order=order, elapsed=ctx.get('elapsed'), polls=ctx.get('polls'),
                settle_polls=ctx.get('settle_polls'), price_source=source))
            payload.update(ctx['extra'])
            return payload
        except Exception as exc:
            _instrumentation_failed('stc_fill_payload', exc)
            return {}

    # ------------------------------------------------------------------ #
    # FC-120 PR-1 — per-leg quote instrumentation (logging only)
    # ------------------------------------------------------------------ #
    def _diag_option_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """One instrumentation option quote, deadline-bounded (ruling B).
        Never raises; None on timeout, exception or an empty answer."""
        return _bounded_read(self.alpaca.get_option_quote, symbol)[0]

    def _flush_deferred(self) -> None:
        """Run the queued post-settle work (ruling A). Each entry is total;
        this loop is too, so a failing entry cannot stop the next one."""
        pending, self._deferred = self._deferred, []
        for work in pending:
            try:
                work()
            except Exception as exc:
                _instrumentation_failed('deferred', exc)

    def _opportunity_quote_set(self, opportunity: Dict[str, Any]) -> Dict[str, Any]:
        """The evaluation-time BTC quote set, rebuilt from the opportunity for
        a skip raised inside execute_roll. No read."""
        return _evaluation_quote_set(
            opportunity.get('btc_quote'), opportunity.get('btc_limit'),
            opportunity.get('underlying', ''), opportunity.get('old_strike'),
            opportunity.get('stock_bid'), opportunity.get('stock_ask'),
            opportunity.get('stock_quote_ts'))

    def _dry_run_stc_fields(self, opportunity: Dict[str, Any]) -> Dict[str, Any]:
        """The STO candidate's quote set for the dry-run event, from the chain
        row it was screened on, every key prefixed ``stc_``. No read."""
        try:
            row = opportunity.get('candidate')
            row = row if isinstance(row, dict) else {}
            quote = {'bid': row.get('bid'), 'ask': row.get('ask'),
                     'bid_size': row.get('bid_size'),
                     'ask_size': row.get('ask_size'),
                     'timestamp': row.get('quote_timestamp')}
            fields = _quote_fields(
                quote, opportunity.get('stc_limit'), 'sell',
                opportunity.get('underlying', ''), opportunity.get('new_strike'),
                opportunity.get('stock_bid'))
            fields['quote_feed'] = None      # the chain call passes no feed
            return {f'stc_{k}': v for k, v in fields.items()}
        except Exception as exc:
            _instrumentation_failed('dry_run_stc_fields', exc)
            return {}

    def _placement_fields(self, opportunity: Dict[str, Any], *, leg: str,
                          quote: Optional[Dict[str, Any]],
                          limit: Any, strike: Any,
                          rung: Optional[int] = None,
                          reused_from_rung: Optional[int] = None,
                          chain_stamp: Any = None) -> Dict[str, Any]:
        """The DD-2 placement set for a leg, computed AT placement (so every
        age — the option quote's and the stock quote's, both from their stored
        broker stamps — is its age when the order went out). Total (ruling C):
        any failure yields the null set plus one breadcrumb, never a raise.

        The re-read set (``quote_reread_*``) is NOT here: it is read after the
        order is placed (ruling A) and merged into the leg's later rows."""
        side = 'buy' if leg == 'btc' else 'sell'
        try:
            fields = _quote_fields(
                quote, limit, side, opportunity.get('underlying', ''), strike,
                opportunity.get('stock_bid'))
            fields.update(_stock_fields(
                opportunity.get('stock_bid'), opportunity.get('stock_ask'),
                opportunity.get('stock_quote_ts')))
        except Exception as exc:
            _instrumentation_failed('placement_fields', exc)
            fields = dict(_NULL_PLACEMENT_FIELDS)
        ctx = opportunity.get('fc120') if isinstance(opportunity, dict) else None
        ctx = ctx if isinstance(ctx, dict) else {}
        fields.update(
            leg=leg,
            pricing_mode=_str_or_none(opportunity.get('pricing_mode')),
            placed_at=_now_iso(),
            roll_id=_str_or_none(ctx.get('roll_id')),
        )
        if leg == 'btc':
            fields['attempt'] = 0          # PR-1 places exactly one BTC
        else:
            fields['rung'] = _int_or_none(rung)
            fields['quote_reused_from_rung'] = _int_or_none(reused_from_rung)
            # The chain row's stamp (rung 1 only). The chain call passes NO
            # feed, so its feed is logged as null rather than implied.
            fields['chain_quote_age_s'] = (_age_s(chain_stamp)
                                           if rung == 1 else None)
            fields['chain_quote_feed'] = None
            fields['old_option_symbol'] = _str_or_none(
                opportunity.get('old_option_symbol'))
            fields['btc_order_id'] = _str_or_none(ctx.get('btc_order_id'))
        return fields

    def _post_settle_quote(self, symbol: str, underlying: str,
                           placed_quote: Optional[Dict[str, Any]],
                           side: str) -> Dict[str, Any]:
        """One fresh option quote and one IEX stock quote AFTER a zero-fill
        leg's disposition is final — and, per ruling A, after its terminal or
        the next rung's placement. Each read is deadline-bounded (ruling B).

        ``quote_drift`` is ``cancel_ask - quote_ask`` on a buy and
        ``quote_bid - cancel_bid`` on a sell: positive means the indicative
        quote moved away from the order over the leg — confounded by the
        underlying's own move and by selection (ruling E), so it never sizes a
        buffer. Total: on any failure the fields are None and one
        ``call_roll_quote_refresh_failed`` breadcrumb is logged on the TRADE
        logger (it is not an error and must not count as one).
        """
        out = dict(_EMPTY_CANCEL_FIELDS)
        try:
            fresh, why = _bounded_read(self.alpaca.get_option_quote, symbol)
            if fresh is None:
                _quote_refresh_failed(symbol, underlying, why)
            else:
                placed = placed_quote if isinstance(placed_quote, dict) else {}
                c_bid, c_ask = _pos(fresh.get('bid')), _pos(fresh.get('ask'))
                out.update(
                    cancel_quote_bid=c_bid, cancel_quote_ask=c_ask,
                    cancel_quote_ts=_iso(fresh.get('timestamp')),
                    cancel_quote_age_s=_age_s(fresh.get('timestamp')),
                    quote_drift=(_diff(c_ask, _pos(placed.get('ask')))
                                 if side == 'buy'
                                 else _diff(_pos(placed.get('bid')), c_bid)))
            stock, _why = _bounded_read(self.alpaca.get_stock_quote, underlying)
            if stock is not None:
                out.update(
                    cancel_stock_bid=_pos(stock.get('bid')),
                    cancel_stock_ask=_pos(stock.get('ask')),
                    cancel_stock_quote_ts=_iso(stock.get('timestamp')))
        except Exception as exc:
            _instrumentation_failed('post_settle_quote', exc)
            return dict(_EMPTY_CANCEL_FIELDS)
        return out

    @staticmethod
    def _fill_fields(*, limit: Any, filled_price: Any, side: str,
                     order: Optional[Dict[str, Any]], elapsed: Optional[float],
                     polls: Optional[int], settle_polls: Optional[int],
                     price_source: Optional[str] = None) -> Dict[str, Any]:
        """``fill_vs_limit`` (>= 0 by construction: ``limit - fill`` on a buy,
        ``fill - limit`` on a sell), broker fill latency and leg wall-clock.

        ``leg_elapsed_s`` is our monotonic clock from just before the place
        call to the disposition, so it is QUANTISED by the 5 s poll: a fill
        that lands 0.2 s after placement reads ~5 s if the first poll missed
        it. ``fill_latency_s`` (the broker's ``filled_at - submitted_at``) is
        the latency. With ``price_source == "fallback"`` (no
        ``filled_avg_price``; the limit stood in) ``fill_vs_limit`` is None —
        a limit compared with itself measures nothing. Total (ruling C)."""
        try:
            lim, fill = _pos(limit), _pos(filled_price)
            if price_source == 'fallback':
                fill_vs_limit = None
            else:
                fill_vs_limit = (_diff(lim, fill) if side == 'buy'
                                 else _diff(fill, lim))
            return {
                'limit_price': lim,
                'fill_vs_limit': fill_vs_limit,
                'fill_price_source': _str_or_none(price_source),
                'fill_latency_s': _fill_latency_s(order),
                'leg_elapsed_s': _num(elapsed),
                'polls': _int_or_none(polls),
                'settle_polls': _int_or_none(settle_polls),
            }
        except Exception as exc:
            _instrumentation_failed('fill_fields', exc)
            return dict(_NULL_FILL_FIELDS)

    def _log_leg_settled(self, *, symbol: str, underlying: str,
                         order_id: Optional[str], disposition: str,
                         placement: Dict[str, Any], limit: Any,
                         requested_qty: int, filled_qty: int = 0,
                         filled_price: Any = None,
                         order: Optional[Dict[str, Any]] = None,
                         cancel: Optional[Dict[str, Any]] = None,
                         elapsed: Optional[float] = None,
                         polls: Optional[int] = None,
                         settle_polls: Optional[int] = None,
                         price_source: Optional[str] = None,
                         extra: Optional[Dict[str, Any]] = None) -> None:
        """``call_roll_leg_settled`` — exactly one row per placed order.

        Per-LEG, informational, never alert-wired, and NOT part of the
        one-terminal-per-position contract. Logging must never break the roll,
        so a failure here is swallowed — with one low-severity
        ``call_roll_instrumentation_failed`` breadcrumb, so a lost row is
        detectable rather than silent (ruling C).
        """
        try:
            filled = _int_or_none(filled_qty) or 0
            side = 'buy' if placement.get('leg') == 'btc' else 'sell'
            payload = dict(placement)
            payload.update(_EMPTY_CANCEL_FIELDS)
            if cancel:
                payload.update(cancel)
            payload.update(self._fill_fields(
                limit=limit,
                filled_price=(filled_price if filled > 0 else None),
                side=side, order=order, elapsed=elapsed, polls=polls,
                settle_polls=settle_polls,
                price_source=(price_source if filled > 0 else None)))
            if extra:
                payload.update(extra)
            payload.update(
                order_id=_str_or_none(order_id) or None,
                order_status=_str_or_none((order or {}).get('status')),
                disposition=disposition,
                filled_price=(_pos(filled_price) if filled > 0 else None),
                filled_qty=filled,
                requested_qty=_int_or_none(requested_qty),
                order_submitted_at=_iso((order or {}).get('submitted_at')),
            )
            log_trade_event(
                logger, event_type="call_roll_leg_settled",
                symbol=symbol, underlying=underlying, strategy="roll_call",
                success=disposition in ('filled', 'partial'), **payload)
        except Exception as exc:
            _instrumentation_failed('leg_settled', exc)

    # ------------------------------------------------------------------ #
    # Order plumbing
    # ------------------------------------------------------------------ #
    def _poll_order_fill(self, order_id: str,
                         timeout: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Poll until the order reaches a terminal status, or the timeout.

        ``partially_filled`` is NOT terminal: returning on it (as-built) hands
        the caller a half-fill while the remainder is still working, which is
        how the STO leg came to be sized off the requested quantity. Neither is
        ``pending_cancel``. Returns None when no terminal status was observed
        inside the bound — which means **we do not know what this order did**,
        and every caller must treat it that way.

        An unreadable order (API error, empty response) is NOT a disposition
        either: it simply fails to advance the poll, and if it never advances
        the caller gets None. The alternative — reading a blip as "verified zero
        fill" — is how a filled contract gets reported as canceled.
        """
        if timeout is None:
            timeout = self.config.rolling_btc_fill_timeout_seconds
        poll_interval = 5
        elapsed = 0
        reported_refetch_failure = False
        # FC-120 PR-1: reads issued by THIS call, for the log only. Callers
        # snapshot it immediately after the primary poll returns, because
        # ``_cancel_and_settle`` re-enters this method and resets it.
        self._last_poll_reads = 0

        while True:
            self._last_poll_reads += 1
            try:
                order = self.alpaca.get_order_by_id(order_id)
                if order and order.get('status', '') in _TERMINAL_ORDER_STATUSES:
                    return order
            except Exception as exc:
                # Once per poll, not once per read: a persistent outage should
                # leave one breadcrumb, not twenty-four.
                if not reported_refetch_failure:
                    reported_refetch_failure = True
                    log_error_event(
                        logger, error_type="call_roll_order_refetch_failed",
                        error_message=str(exc), component="call_roller",
                        recoverable=True, order_id=order_id,
                    )
            if elapsed >= timeout:
                return None
            time.sleep(poll_interval)
            elapsed += poll_interval

    def _cancel_and_settle(self, order_id: str) -> Optional[Dict[str, Any]]:
        """Cancel an order, then poll until the broker SETTLES it.

        FC-078 review, execution H-1. Alpaca cancels are queued: the order goes
        to ``pending_cancel`` and can still fill from there. A single re-read
        after the cancel therefore establishes nothing — the shipped version
        dispatched on ``filled_qty`` while ignoring a non-terminal status, which
        produced two documented fictions in the reviewer's probe: an STO ladder
        that placed three sells which all filled while reporting
        ``naked_exposure`` ("no naked position") against two genuinely naked
        calls, and a BTC reporting ``btc_timeout_canceled`` with
        ``order_status=pending_cancel`` while the DAY order kept working.

        Returns the terminal order dict, or **None when the disposition could
        not be established**. None is not "nothing filled": it is "we do not
        know", and callers fail safe on it — never placing another sell, always
        emitting the alert-wired unknown-disposition terminal.
        """
        self._safe_cancel(order_id)
        return self._poll_order_fill(
            order_id, timeout=_CANCEL_SETTLE_TIMEOUT_SECONDS)

    def _safe_cancel(self, order_id: str) -> bool:
        """Cancel, swallowing failure. A failed cancel is never conclusive — the
        caller settles the order afterwards, which is what decides the
        disposition."""
        try:
            return bool(self.alpaca.cancel_order(order_id))
        except Exception:
            return False


def _as_float(value: Any) -> float:
    """Coerce a quote/order field to a float, treating junk as 0.0."""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------- #
# FC-120 PR-1 — quote instrumentation (docs/plans/fc-120.md DD-2).
#
# LOGGING ONLY. Nothing below feeds a limit, an order argument or a branch; the
# roller prices exactly as it did before these helpers existed. Every value they
# return is a JSON primitive (str / int / float / bool / None) — never a
# datetime, Decimal or dict — because the log sink turns each field into a
# BigQuery column and a datetime reaching structlog is a repr, not a value.
# Differences go through ``_dec`` (exact decimal via str), never
# ``Decimal(float)``: ``Decimal(6.95) % Decimal("0.05")`` is 1.78e-16, not 0.
# --------------------------------------------------------------------------- #

#: The post-settle set on a leg that did not end zero-fill (or whose refresh
#: failed). Spelled once so every row carries the same keys.
_EMPTY_CANCEL_FIELDS: Dict[str, Any] = {
    'cancel_quote_bid': None, 'cancel_quote_ask': None,
    'cancel_quote_ts': None, 'cancel_quote_age_s': None, 'quote_drift': None,
    'cancel_stock_bid': None, 'cancel_stock_ask': None,
    'cancel_stock_quote_ts': None,
}

#: The re-read set, merged into a leg's rows once the order is out (ruling A).
_REREAD_KEYS = ('quote_reread_bid', 'quote_reread_ask', 'quote_reread_ts',
                'quote_reread_same_tick', 'quote_reread_delta')

_QUOTE_KEYS = ('quote_bid', 'quote_ask', 'quote_mid', 'quote_spread',
               'quote_bid_size', 'quote_ask_size', 'quote_ts', 'quote_age_s',
               'quote_feed', 'limit_on_tick', 'limit_vs_quote',
               'intrinsic_at_placement', 'limit_minus_intrinsic')
_STOCK_KEYS = ('stock_bid', 'stock_ask', 'stock_quote_ts', 'stock_quote_age_s',
               'stock_bid_ts')

#: What a placement set degrades to when building it failed (ruling C).
_NULL_PLACEMENT_FIELDS: Dict[str, Any] = {
    k: None for k in _QUOTE_KEYS + _STOCK_KEYS}

#: What the fill set degrades to when building it failed (ruling C).
_NULL_FILL_FIELDS: Dict[str, Any] = {
    'limit_price': None, 'fill_vs_limit': None, 'fill_price_source': None,
    'fill_latency_s': None, 'leg_elapsed_s': None, 'polls': None,
    'settle_polls': None}


def _num(value: Any) -> Optional[float]:
    """A finite float, or None. Never 0.0 for a missing value, never a raise
    (an int too large for a float is None, not an ``OverflowError``)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except Exception:
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _pos(value: Any) -> Optional[float]:
    """A strictly positive finite float, or None (a quote side of 0 is absent)."""
    out = _num(value)
    return out if out is not None and out > 0 else None


def _int_or_none(value: Any) -> Optional[int]:
    out = _num(value)
    try:
        return int(out) if out is not None else None
    except Exception:
        return None


def _str_or_none(value: Any) -> Optional[str]:
    """A non-empty string as itself; anything else (incl. a Mock) is None."""
    return value if isinstance(value, str) and value else None


def _diff(a: Optional[float], b: Optional[float]) -> Optional[float]:
    """``a - b`` exactly (decimal), as a finite float; None if either side is
    absent or the result is not a finite number. Never raises."""
    if a is None or b is None:
        return None
    try:
        return _num(float(_dec(a) - _dec(b)))
    except Exception:
        return None


def _safe_diff(a: Any, b: Any) -> Optional[float]:
    """``_diff`` over raw values (coerced through ``_pos``)."""
    return _diff(_pos(a), _pos(b))


def _safe_merge(base: Any, overrides: Dict[str, Any]) -> Dict[str, Any]:
    """``dict(base)`` updated with ``overrides``; a non-dict base is empty."""
    out = dict(base) if isinstance(base, dict) else {}
    out.update(overrides)
    return out


def _parse_stamp(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        except Exception:
            return None
    return None


def _iso(value: Any) -> Optional[str]:
    """A broker stamp as an ISO string. A datetime is formatted, a string that
    parses as ISO-8601 passes through, anything else (junk included) is None."""
    try:
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if _parse_stamp(value) is not None:
            return value.strip()
    except Exception:
        return None
    return None


def _age_s(stamp: Any) -> Optional[float]:
    """Seconds since the BROKER's stamp (not our clock); None if unreadable."""
    try:
        return _num(quote_age_seconds(stamp))
    except Exception:
        return None


def _same_tick(a: Any, b: Any) -> Optional[bool]:
    """Do two quote stamps name the same instant? None unless both parse.

    True says only that the server returned the SAME stored quote twice — it
    is not evidence that the feed is a deterministic transform of the NBBO."""
    try:
        pa, pb = _parse_stamp(a), _parse_stamp(b)
        if pa is None or pb is None:
            return None
        if (pa.tzinfo is None) != (pb.tzinfo is None):
            return None
        return pa == pb
    except Exception:
        return None


def _now_iso() -> Optional[str]:
    try:
        return clock.now_utc().isoformat()
    except Exception:
        return None


def _limit_on_tick(limit: Any, underlying: Any) -> Optional[bool]:
    """Is ``limit`` on the legal tick grid for ``underlying``? None if no
    limit, or if the question cannot be answered (a 1e308 limit raises
    ``decimal.InvalidOperation``; a non-string root has no tick class)."""
    value = _pos(limit)
    if value is None or not isinstance(underlying, str):
        return None
    try:
        exact = _dec(value)
        return bool(exact % tick_size(exact, underlying) == 0)
    except Exception:
        return None


def _fill_latency_s(order: Optional[Dict[str, Any]]) -> Optional[float]:
    """Alpaca ``filled_at - submitted_at`` in seconds; None if either is absent."""
    try:
        if not isinstance(order, dict):
            return None
        filled = _parse_stamp(order.get('filled_at'))
        submitted = _parse_stamp(order.get('submitted_at'))
        if filled is None or submitted is None:
            return None
        return _num(round((filled - submitted).total_seconds(), 3))
    except Exception:
        return None


def _fill_price_source(order: Any) -> str:
    """``"broker"`` when the order carries a usable ``filled_avg_price``,
    ``"fallback"`` when the roller had to stand the limit in for it."""
    try:
        price = order.get('filled_avg_price') if isinstance(order, dict) else None
    except Exception:
        price = None
    return 'broker' if _pos(price) is not None else 'fallback'


def _zero_fill_disposition(status: Any, timed_out: bool) -> str:
    """The TRUE disposition of a leg that ended with zero fill, from the
    broker's status: our cancel landing is ``timeout_canceled``; a broker
    refusal is ``rejected``; anything else (``expired``, or a cancel we did not
    make) is ``terminal_no_fill``."""
    if status == 'rejected':
        return 'rejected'
    if status == 'canceled' and timed_out:
        return 'timeout_canceled'
    return 'terminal_no_fill'


def _quote_fields(quote: Optional[Dict[str, Any]], limit: Any, side: str,
                  underlying: Any, strike: Any, stock_bid: Any) -> Dict[str, Any]:
    """The DD-2 placement set for one leg: the quote the limit was priced from.

    ``side`` is ``"buy"`` (a BTC) or ``"sell"`` (an STO). Sign conventions, so
    ">= 0" always means the same thing: ``limit_vs_quote`` is ``limit - ask``
    on a buy and ``bid - limit`` on a sell (>= 0 = at or through the quoted
    side). ``intrinsic_at_placement`` is ``max(0, stock_bid - strike)`` from
    the EVALUATION-time IEX stock bid (its stamp is ``stock_bid_ts``; PR-1
    makes no stock re-read before an order) — a BTC limit below it is one no
    NBBO ask could match.

    The roller's limits are deliberately NOT priced through ``limit_pricing``
    (docs/CLAUDE.md); only ``_dec`` / ``tick_size`` / ``quote_age_seconds`` are
    borrowed here, to describe a limit, never to set one. Total: any failure
    is the null set (the caller logs the breadcrumb).
    """
    q = quote if isinstance(quote, dict) else {}
    bid, ask = _pos(q.get('bid')), _pos(q.get('ask'))
    two_sided = bid is not None and ask is not None
    lim = _pos(limit)
    buy = side == 'buy'
    stock, k = _pos(stock_bid), _pos(strike)
    intrinsic = (_diff(stock, k) if stock is not None and k is not None
                 else None)
    if intrinsic is not None:
        intrinsic = max(0.0, intrinsic)
    mid = _num(float((_dec(bid) + _dec(ask)) / 2)) if two_sided else None
    feed = q.get('feed')
    return {
        'quote_bid': bid,
        'quote_ask': ask,
        'quote_mid': mid,
        'quote_spread': (_diff(ask, bid) if two_sided else None),
        'quote_bid_size': _int_or_none(q.get('bid_size')),
        'quote_ask_size': _int_or_none(q.get('ask_size')),
        'quote_ts': _iso(q.get('timestamp')),
        'quote_age_s': _age_s(q.get('timestamp')),
        'quote_feed': feed if isinstance(feed, str) else None,
        'limit_on_tick': _limit_on_tick(lim, underlying),
        'limit_vs_quote': (_diff(lim, ask) if buy else _diff(bid, lim)),
        'intrinsic_at_placement': intrinsic,
        'limit_minus_intrinsic': _diff(lim, intrinsic),
    }


def _reread_fields(quote: Any, reread: Any, side: str) -> Dict[str, Any]:
    """The re-read set: a second read of the same symbol, taken right AFTER
    the order was placed (ruling A), so it is not same-instant with the read
    the limit was priced from. ``quote_reread_delta`` is ``reread_ask - ask``
    on a buy and ``bid - reread_bid`` on a sell. ``quote_reread_same_tick``
    says whether both reads carry the same broker stamp: a delta of 0.00 WITH
    the same stamp shows only that the server returned the same stored quote
    twice — not that the feed is a deterministic transform. Total."""
    try:
        q = quote if isinstance(quote, dict) else {}
        r = reread if isinstance(reread, dict) else {}
        bid, ask = _pos(q.get('bid')), _pos(q.get('ask'))
        r_bid, r_ask = _pos(r.get('bid')), _pos(r.get('ask'))
        return {
            'quote_reread_bid': r_bid,
            'quote_reread_ask': r_ask,
            'quote_reread_ts': _iso(r.get('timestamp')),
            'quote_reread_same_tick': _same_tick(q.get('timestamp'),
                                                 r.get('timestamp')),
            'quote_reread_delta': (_diff(r_ask, ask) if side == 'buy'
                                   else _diff(bid, r_bid)),
        }
    except Exception as exc:
        _instrumentation_failed('reread_fields', exc)
        return {k: None for k in _REREAD_KEYS}


def _stock_fields(stock_bid: Any, stock_ask: Any, stock_quote_ts: Any) -> Dict[str, Any]:
    """The evaluation-time IEX stock quote, its age computed NOW from its own
    stored broker stamp — the same time base as ``quote_age_s``."""
    ts = _iso(stock_quote_ts)
    return {'stock_bid': _pos(stock_bid), 'stock_ask': _pos(stock_ask),
            'stock_quote_ts': ts, 'stock_quote_age_s': _age_s(ts),
            'stock_bid_ts': ts}


def _evaluation_quote_set(quote: Any, limit: Any, underlying: Any, strike: Any,
                          stock_bid: Any, stock_ask: Any,
                          stock_quote_ts: Any) -> Dict[str, Any]:
    """The BTC quote set as of evaluation (first read + stock quote; no extra
    read), for ``call_roll_evaluated`` and the skips after the BTC read. Total."""
    try:
        out = _quote_fields(quote, limit, 'buy', underlying, strike, stock_bid)
        out.update(_stock_fields(stock_bid, stock_ask, stock_quote_ts))
        return out
    except Exception as exc:
        _instrumentation_failed('evaluation_quote_set', exc)
        return dict(_NULL_PLACEMENT_FIELDS)


def _bounded_read(fn: Callable[..., Any], *args: Any,
                  timeout: Optional[float] = None
                  ) -> Tuple[Optional[Dict[str, Any]], str]:
    """Run ONE instrumentation read under a hard wall-clock cap (ruling B).

    The read runs in a daemon worker thread; the caller waits at most
    ``_DIAG_READ_TIMEOUT_SECONDS`` (read at call time) and then walks away —
    a read that never returns is abandoned, never awaited, so it cannot delay
    or suppress anything. Returns ``(quote_dict, "ok")``, or ``(None, why)``
    with ``why`` in ``timeout`` / ``error`` / ``empty``. Never raises.

    The simulated clock is thread-local (``src/utils/clock.py``), so a frozen
    caller's time is handed to the worker: in a replay the adapter answers for
    the simulated day, exactly as it would on the caller's thread.
    """
    limit = _DIAG_READ_TIMEOUT_SECONDS if timeout is None else timeout
    box: Dict[str, Any] = {}
    try:
        frozen = clock.now() if clock.is_frozen() else None

        def work() -> None:
            try:
                if frozen is not None:
                    clock.set_now(frozen)
                box['value'] = fn(*args)
            except BaseException as exc:   # noqa: B036 - a worker must not die loud
                box['error'] = exc

        worker = threading.Thread(target=work, name='fc120-diag-read',
                                  daemon=True)
        worker.start()
        worker.join(limit)
        if worker.is_alive():
            return None, 'timeout'
    except Exception:
        return None, 'error'
    if 'error' in box:
        return None, 'error'
    value = box.get('value')
    if isinstance(value, dict) and value:
        return value, 'ok'
    return None, 'empty'


def _instrumentation_failed(where: str, exc: BaseException) -> None:
    """One low-severity breadcrumb for a swallowed instrumentation failure, so
    a lost or nulled row is detectable (ruling C). Never raises."""
    try:
        # INFO, category ``system``, with an explicit ``event_type`` so the
        # row is one ``jsonPayload.event_type`` query away (log_system_event
        # sets only the message).
        logger.info(
            "call_roll_instrumentation_failed", event_category="system",
            event_type="call_roll_instrumentation_failed", where=where,
            error_class=type(exc).__name__, error=str(exc)[:200])
    except Exception:
        pass


def _quote_refresh_failed(symbol: str, underlying: Any, why: str) -> None:
    """The post-settle quote could not be read. On the TRADE logger, not the
    error logger: it is a missing diagnostic, not an error, and must not count
    in ``errors_all`` / ``total_errors``. Never raises."""
    try:
        log_trade_event(
            logger, event_type="call_roll_quote_refresh_failed",
            symbol=symbol, underlying=_str_or_none(underlying),
            strategy="roll_call", success=False, reason=why)
    except Exception:
        pass


def _start_roll_ctx(opportunity: Dict[str, Any]) -> None:
    """Stamp a fresh ``roll_id`` (one per ``execute_roll`` call) on the
    opportunity. It keys every leg row of this roll together. Log-only."""
    try:
        opportunity['fc120'] = {'roll_id': uuid.uuid4().hex,
                                'btc_order_id': None}
    except Exception:
        pass


def _roll_ctx(opportunity: Dict[str, Any]) -> Dict[str, Any]:
    """The opportunity's FC-120 context (a throwaway dict if it is missing)."""
    ctx = opportunity.get('fc120') if isinstance(opportunity, dict) else None
    return ctx if isinstance(ctx, dict) else {}


def _note_skip(roller: 'CallRoller', option_symbol: Any, reason: Any) -> None:
    """Remember a position's skip reason for the end-of-cycle sampler."""
    try:
        if isinstance(option_symbol, str) and option_symbol:
            roller.skip_reasons[option_symbol] = str(reason)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# FC-120 PR-1 (ruling D) — the end-of-cycle quote sampler. READ-ONLY.
# --------------------------------------------------------------------------- #

#: The sampler runs only when the roll cycle finished inside this many seconds.
QUOTE_SAMPLE_MAX_CYCLE_SECONDS = 600


def sample_short_call_quotes(alpaca: Any, option_symbols: List[str],
                             skip_reasons: Dict[str, str],
                             cycle_started_monotonic: float) -> int:
    """Emit one ``call_roll_quote_sample`` row per held short call.

    Called by ``WheelEngine.run_rolling_cycle`` after every position has been
    processed and every terminal emitted. Per position: two back-to-back
    indicative option quotes and the IEX stock quote, each deadline-bounded
    (ruling B). No order, no effect on any result; every failure is silent but
    for one ``call_roll_instrumentation_failed`` breadcrumb. It exists because
    most cycles never place an order, so the per-leg rows alone would leave the
    DD-1 buffer read starved: this row measures the feed every cycle —
    ``ask_minus_intrinsic`` (a quoted ask below parity is the modified-quote
    case no buffer bounds) and the re-read pair (``same_tick``; deltas).

    Returns the number of rows emitted (for tests); never raises.
    """
    emitted = 0
    try:
        for option_symbol in option_symbols:
            elapsed = time.monotonic() - cycle_started_monotonic
            if elapsed >= QUOTE_SAMPLE_MAX_CYCLE_SECONDS:
                break
            if _sample_one(alpaca, option_symbol,
                           skip_reasons.get(option_symbol), elapsed):
                emitted += 1
    except Exception as exc:
        _instrumentation_failed('quote_sample', exc)
    return emitted


def _sample_one(alpaca: Any, option_symbol: Any, skip_reason: Any,
                elapsed: float) -> bool:
    try:
        parsed = parse_option_symbol(option_symbol)
        underlying = parsed.get('underlying', '')
        strike = _pos(parsed.get('strike_price'))
        first, _w1 = _bounded_read(alpaca.get_option_quote, option_symbol)
        second, _w2 = _bounded_read(alpaca.get_option_quote, option_symbol)
        stock, _w3 = _bounded_read(alpaca.get_stock_quote, underlying)
        q1 = first or {}
        q2 = second or {}
        s = stock or {}
        bid1, ask1 = _pos(q1.get('bid')), _pos(q1.get('ask'))
        bid2, ask2 = _pos(q2.get('bid')), _pos(q2.get('ask'))
        stock_bid = _pos(s.get('bid'))
        intrinsic = (_diff(stock_bid, strike)
                     if stock_bid is not None and strike is not None else None)
        if intrinsic is not None:
            intrinsic = max(0.0, intrinsic)
        feed = q1.get('feed')
        log_trade_event(
            logger, event_type="call_roll_quote_sample",
            symbol=option_symbol, underlying=_str_or_none(underlying),
            strategy="roll_call", success=True,
            strike=strike,
            quote_bid=bid1, quote_ask=ask1,
            quote_ts=_iso(q1.get('timestamp')),
            quote_age_s=_age_s(q1.get('timestamp')),
            quote_reread_bid=bid2, quote_reread_ask=ask2,
            quote_reread_ts=_iso(q2.get('timestamp')),
            quote_reread_age_s=_age_s(q2.get('timestamp')),
            same_tick=_same_tick(q1.get('timestamp'), q2.get('timestamp')),
            quote_reread_delta_bid=_diff(bid2, bid1),
            quote_reread_delta_ask=_diff(ask2, ask1),
            quote_feed=feed if isinstance(feed, str) else None,
            stock_bid=stock_bid, stock_ask=_pos(s.get('ask')),
            stock_quote_ts=_iso(s.get('timestamp')),
            intrinsic=intrinsic,
            ask_minus_intrinsic=_diff(ask1, intrinsic),
            limit_on_tick=_limit_on_tick(ask1, underlying),
            cycle_skip_reason=_str_or_none(skip_reason),
            cycle_elapsed_s=_num(round(elapsed, 1)),
        )
        return True
    except Exception as exc:
        _instrumentation_failed('quote_sample', exc)
        return False
