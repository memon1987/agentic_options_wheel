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

    call_roll_skipped{skip_reason}   no order was ever placed (FC-120 PR-2 adds
                                     ``quote_unusable``: both BTC quotes unusable
                                     at execute time, before any order)
    call_roll_btc_rejected           the BTC was refused; nothing live
    call_roll_btc_timeout_canceled   canceled with VERIFIED zero fill — or a
                                     re-price was refused after the roller's own
                                     cancel (``reprice_skipped_reason``)
    call_roll_naked_exposure         some BTC qty filled, STO ladder exhausted
    call_roll_completed              both legs done
    call_roll_dry_run                ROLLER_DRY_RUN=true; nothing placed

``call_roll_leg_settled`` (FC-120 PR-1) is a per-LEG row — exactly one per
placed order, at that order's disposition — and is deliberately NOT part of
this contract: a position that placed a BTC and two STO rungs emits three of
them and still exactly one terminal. ``call_roll_stc_timeout_canceled`` and
``call_roll_btc_repriced`` (PR-2) are per-rung / per-attempt for the same
reason. All three are informational and never alert-wired.

FC-120 PR-2 — the pricing rule (docs/plans/fc-120.md DD-3/DD-4). Base-mode
limits are buffered THROUGH the quoted side and TICK-LEGAL by construction,
priced from a bounded quote read immediately before each order: the
buy-to-close at ``snap_up(max(ask + buffer, parity + 0.01))`` (parity from the
IEX stock bid — a lower bound, never below intrinsic, not a marketability
guarantee), the sell-to-open at ``snap_down(bid - buffer)``, never below the
floor ``snap_up(btc_fill + min_credit)``. Imminence keeps its pad on each leg's
FIRST placement; every re-price is base-mode. The credit invariant is tested on
the buffered, snapped limits at every site, so a filled roll still cannot net a
debit. Before any order exists, both limits come from fresh execute-time reads;
an unusable new-symbol read is ``credit_gone_at_execution``, as on main — the
pre-BTC screen is never priced off the chain row (review finding F5). A
buy-to-close is re-priced (up to ``btc_reprice_attempts`` times, inside
``btc_fill_timeout_seconds``) ONLY after the roller's own cancel-and-settle
returned a terminal zero fill (``canceled``), and a re-price STRICTLY improves
on the limit it replaces (``max(formula, prior + one tick)`` — F1); a
``rejected`` is terminal, and a terminal zero fill from the PRIMARY poll
(``expired``, or canceled by another actor) stays terminal — so a replay, whose
adapter answers ``expired`` on the first read, never re-prices. The STO ladder
is rung 1 (a ``stc_rung_timeout_seconds`` window), ``stc_escalation_rungs``
escalation rungs (shipped 0; placed only after the roller's own zero-fill
settle or a synchronous rejection), the floor, then the fallbacks. Every
placement salts its ``client_order_id`` with ``roll_id`` + leg + attempt/rung
(F1), so no two orders of one roll can collide on Alpaca's idempotency key.

Instrumentation never sits on the order path (rev-4 ruling A): the order-path
broker calls are pricing reads, place, poll and cancel, in that structure. A
leg's diagnostic re-read follows its placement; a zero-fill leg's post-settle
quote follows its terminal event or the next placement, and its fields ride on
the later ``call_roll_leg_settled`` row only. EVERY bounded read — diagnostic
(2.0 s cap) and pricing (3.0 s cap, each with a stated fallback) — runs in a
daemon worker on the quiet data plane (ruling B; R6-J).
``call_roll_quote_sample`` (ruling D) is emitted by the cycle after every
terminal, never by ``execute_roll``.

Events must tell the truth about what filled. A cancel that fails *because the
order filled* is a fill, not an error — which is why every cancel on this path
is followed by a re-fetch before anything is reported.
"""

import math
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Callable, Dict, Any, Iterator, List, Optional, Set, Tuple

import structlog

from ..api.alpaca_client import AlpacaClient, quiet_data_plane
from ..api.market_data import MarketDataManager
from ..api.earnings_calendar import (
    EarningsCalendarService, EARNINGS_KNOWN, EARNINGS_UNKNOWN)
from ..risk.risk_manager import RiskManager
from ..utils.config import Config
from ..utils.option_symbols import parse_option_symbol, coerce_expiry_date
from ..utils.logging_events import log_trade_event, log_error_event, log_position_update
from ..utils import clock
from .cost_basis import CostBasisResolver, SOURCE_DIVERGENT
# FC-120: exact decimals and tick LEGALITY only. The roller's limits are NOT
# priced by ``sell_limit_price`` (an opening write can rest at mid; a roll's
# legs must execute in one session) — PR-2 borrows ``tick_size`` and
# ``snap_limit`` to make its own marketable limits tick-legal.
from .limit_pricing import (_dec, _warn_if_unverified, quote_age_seconds,
                            snap_limit, tick_size)
from . import roll_budget

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

# The timing constants live in the leaf ``roll_budget`` (FC-120 PR-2, DD-4) —
# the per-position budget is computed from them — and are re-exported here
# under their historical names, so existing imports and monkeypatches keep
# working. Every reader below reads THIS module's binding at call time.
#
# How long to keep re-reading an order after cancelling it, waiting for the
# broker to settle it into a terminal status. Short: a cancel that has not
# resolved in this window is not going to tell us anything by waiting longer,
# and the safe action does not depend on the answer — we stop touching the
# position either way. A FLOOR, not a cap: the settle also reads the order at
# least ``_CANCEL_SETTLE_MIN_READS`` times however slow each read is (R6-I).
_CANCEL_SETTLE_TIMEOUT_SECONDS = roll_budget.CANCEL_SETTLE_TIMEOUT_SECONDS
_CANCEL_SETTLE_MIN_READS = roll_budget.CANCEL_SETTLE_MIN_READS
_POLL_INTERVAL_SECONDS = roll_budget.POLL_INTERVAL_SECONDS

# FC-120 PR-1 (ruling B): hard wall-clock cap on EVERY instrumentation read.
# The read runs in a daemon worker; a read that has not returned inside this
# bound is abandoned and logged as None, so a hung quote endpoint can neither
# delay nor suppress an order action or a terminal event.
_DIAG_READ_TIMEOUT_SECONDS = roll_budget.DIAG_READ_TIMEOUT_SECONDS

# FC-120 PR-2: the same mechanism for PRICING reads (a limit is computed from
# them). Each has a stated fallback — the evaluation quote, the prior basis,
# the pre-BTC quote, the floor — so a hung quote endpoint costs at most this
# per read and never suppresses an order or a terminal.
_PRICING_READ_TIMEOUT_SECONDS = roll_budget.PRICING_READ_TIMEOUT_SECONDS

#: Zero-fill dispositions that are a MISS (the order sat and did not fill), as
#: opposed to a refusal. Only a miss is a measurement of where the market was.
_MISS_DISPOSITIONS = ('timeout_canceled', 'terminal_no_fill')

#: ``_post_settle_quote``'s "no option read was taken for this leg yet" — as
#: opposed to ``None``, "a read was taken (a re-price or escalation basis
#: read) and it failed", which must NOT be retried as a second read.
_NOT_PREFETCHED = object()


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
        #
        # FC-120 PR-2 (DD-3): this is the SCREENING limit — buffered, parity-
        # floored and tick-snapped exactly as the order will be, so the credit
        # screen below is tested on what would be placed. The order itself is
        # re-priced from a fresh read at execute time (_price_legs_at_execution).
        buffer = self.config.rolling_marketable_buffer_per_share
        btc_limit = self._btc_limit_from_quote(
            btc_ask, formula=pricing_mode, mid=option_mid, stock_bid=stock_bid,
            strike=current_strike, buffer_multiple=1, buffer=buffer,
            underlying=underlying)
        if btc_limit is None:
            # Unreachable on a real quote (ask > 0 is checked above and
            # imminence needs both sides), kept so a priced-to-nothing limit is
            # a terminal skip rather than a None compared with a credit.
            self.log_terminal_skip(
                option_symbol, underlying, "btc_quote_unavailable",
                current_strike=current_strike,
                stock_price=current_stock_price, **earnings_info)
            return None

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
        # $/contract -> $/share (R6-L: the Config value is DOLLARS PER CONTRACT).
        min_credit_per_share = self.config.rolling_min_net_credit_per_contract / 100.0
        legal = self._legal_candidates(
            candidates, current_strike, cost_basis_per_share, max_expiry,
            btc_limit, min_credit_per_share, imminent, underlying)

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
                          imminent: bool,
                          underlying: str = '') -> List[Dict[str, Any]]:
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
        formula = 'imminence' if imminent else 'base'
        buffer = self.config.rolling_marketable_buffer_per_share
        for candidate in candidates:
            valid, _reason = self.risk_manager.validate_roll(
                candidate, current_strike, cost_basis_per_share, max_expiry)
            if not valid:
                continue

            # FC-120 PR-2: the BUFFERED, snapped limit — the invariant below is
            # tested on what rung 1 would place, never on the raw quote.
            stc_limit = self._stc_limit_from_quote(
                _as_float(candidate.get('bid')), _as_float(candidate.get('ask')),
                formula, buffer_multiple=1, buffer=buffer, floor=None,
                underlying=underlying)
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
    def _btc_limit_from_quote(ask: Any, *, formula: str, mid: Any,
                              stock_bid: Any, strike: Any,
                              buffer_multiple: int, buffer: Any,
                              underlying: str) -> Optional[float]:
        """The buy-to-close limit (FC-120 PR-2 DD-3), or None if unpriceable.

        Base: ``snap_up(max(ask + buffer_multiple x buffer, parity + 0.01))``.
        Imminence: ``snap_up(max(mid + 0.05, parity + 0.01))`` — the pad does
        not stack with the buffer (imminence already crosses half the spread),
        but the parity floor DOES apply: it is a floor, not a pad.
        ``parity = stock_bid - strike`` from the IEX stock bid. ``parity +
        0.01`` is a LOWER BOUND — the limit is never below intrinsic at the
        IEX bid, where no ask could ever match it — not a marketability
        guarantee (F7): the true ask also carries time value, so it binds
        usefully when the indicative ask is quoted below parity. A missing
        stock bid omits the term. The tick is decided from the UNSNAPPED value.
        Every operand is an exact decimal (``_dec``); never ``Decimal(float)``.

        None when ``ask <= 0`` in base mode, or there is no mid in imminence.
        This static and ``_stc_limit_from_quote`` are the named monkeypatch
        SEAM the replay's never-re-price tests inject through (R6-B).
        """
        raw = _btc_raw(ask, formula=formula, mid=mid, stock_bid=stock_bid,
                       strike=strike, buffer_multiple=buffer_multiple,
                       buffer=buffer)
        if raw is None:
            return None
        _warn_if_unverified(underlying)
        return float(snap_limit(raw[0], tick_size(raw[0], underlying), "up"))

    @staticmethod
    def _stc_limit_from_quote(bid: Any, ask: Any, formula: Any, *,
                              buffer_multiple: int, buffer: Any,
                              floor: Any, underlying: str) -> Optional[float]:
        """The sell-to-open limit for a pricing formula, or None if unquotable.

        Base (FC-120 PR-2 DD-3): ``snap_down(bid - buffer_multiple x buffer)``
        — marketable against an indicative bid that sits off the NBBO, and
        tick-legal. Imminence: ``snap_down(mid - 0.05)`` (needs a two-sided
        quote; without one the candidate is dropped rather than silently priced
        by another formula's rule). With ``floor`` given (every rung after the
        buy-to-close filled) the result is ``max(limit, floor)`` — never below
        the invariant — and a usable quote whose formula price is <= 0 is the
        floor itself. Without ``floor`` (the candidate screen and the execute
        re-check) a non-positive price is None.
        """
        if formula == 'imminence':
            b, a = _pos(bid), _pos(ask)
            if b is None or a is None:
                return None
            raw = (_dec(b) + _dec(a)) / 2 - _dec(_IMMINENCE_PAD)
        else:
            b = _pos(bid)
            if b is None:
                return None
            raw = _dec(b) - Decimal(str(buffer)) * buffer_multiple
        limit: Optional[float] = None
        if raw > 0:
            _warn_if_unverified(underlying)
            snapped = snap_limit(raw, tick_size(raw, underlying), "down")
            limit = float(snapped) if snapped > 0 else None
        if floor is not None:
            floor_f = float(floor)
            return floor_f if limit is None or limit < floor_f else limit
        return limit

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
        returned, or right after the NEXT order is placed. It never sits
        between a disposition and its terminal, or between two orders.

        If the body RAISES, the queue is KEPT (FC-120 PR-2, confirmation item
        2): an escaped exception is its own terminal
        (``call_roll_execution_error``, emitted by the cycle) and nothing may
        precede it, so the cycle calls :meth:`flush_deferred` AFTER that
        terminal rather than this wrapper flushing in a ``finally``.
        """
        self._deferred = []
        result = self._execute_roll(opportunity)
        self._flush_deferred()
        return result

    def flush_deferred(self) -> None:
        """Run any instrumentation still queued by a body that raised.

        ``run_rolling_cycle`` calls it after ``call_roll_execution_error`` so a
        leg already placed keeps its ``call_roll_leg_settled`` row. Total."""
        self._flush_deferred()

    def _execute_roll(self, opportunity: Dict[str, Any]) -> Dict[str, Any]:
        """The body of :meth:`execute_roll` — order actions and terminals."""
        underlying = opportunity['underlying']
        old_symbol = opportunity['old_option_symbol']
        new_symbol = opportunity['new_option_symbol']
        contracts = opportunity['contracts']
        earnings_info = opportunity.get('earnings_info', {})

        _start_roll_ctx(opportunity)

        # --- Execute-time pricing (FC-120 PR-2 DD-3) ---
        # Evaluation quotes are seconds-to-minutes old by order time (PR-1
        # measured 3-6 s at placement, already moved). Both limits are
        # re-derived from three bounded reads taken now, and the invariant is
        # re-run on the pair — losing the race HERE costs nothing: no order
        # exists yet.
        if not self._price_legs_at_execution(opportunity):
            return {'success': False,
                    'reason': _roll_ctx(opportunity).get('skip_reason'),
                    'underlying': underlying}
        btc_limit = opportunity['btc_limit']

        if self.config.roller_dry_run:
            # The dry run carries the EXECUTE-TIME-priced limits and the quotes
            # they were priced from (BTC set + the STO rung-1 quote as
            # ``stc_*``), so the limits can be checked off this one event.
            # It returns before any order is placed.
            pricing = opportunity.get('btc_pricing') or {}
            payload = self._placement_fields(
                opportunity, leg='btc', quote=pricing.get('quote'),
                limit=btc_limit, strike=opportunity.get('old_strike'))
            payload.update(_btc_pricing_fields(pricing))
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

        # === LEG 1: Buy-to-close (placed, polled, re-priced — DD-4) ===
        btc_order, btc_leg = self._place_and_settle_btc(opportunity)
        if btc_order is None:
            # The leg's terminal has already been emitted (exactly one).
            return btc_leg
        btc_order_id = btc_leg['btc_order_id']
        btc_filled_qty = btc_leg['btc_filled_qty']
        btc_filled_price = btc_leg['btc_filled_price']

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
                # FC-120 PR-2 (T-16): the replay's pricing-mode split (DD-6)
                # and the cycle's quote sampler (old -> new symbol) read these.
                'pricing_mode': opportunity.get('pricing_mode'),
                'old_option_symbol': old_symbol,
                'new_option_symbol': stc_result.get('symbol', new_symbol),
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
    # FC-120 PR-2 — execute-time pricing (DD-3)
    # ------------------------------------------------------------------ #
    def _pricing_read(self, fn: Callable[..., Any], *args: Any
                      ) -> Optional[Dict[str, Any]]:
        """One bounded PRICING read: a daemon worker on the quiet data plane,
        abandoned after ``_PRICING_READ_TIMEOUT_SECONDS`` (read at call time).
        None on timeout, exception or an empty answer — every caller has a
        stated fallback. Never raises."""
        return _bounded_read(fn, *args, timeout=_PRICING_READ_TIMEOUT_SECONDS,
                             kind='pricing')[0]

    def _price_btc(self, candidates: List[Tuple[Any, str]], *, formula: str,
                   buffer_multiple: int, stock_bid: Optional[float],
                   strike: Any, buffer: Any, underlying: str
                   ) -> Optional[Tuple[float, Dict[str, Any], str, Optional[bool]]]:
        """``(limit, quote, quote_source, parity_floor_applied)`` from the first
        candidate quote that prices, or None when none does."""
        for quote, source in candidates:
            if not isinstance(quote, dict) or not quote:
                continue
            mid = _mid(quote)
            limit = self._btc_limit_from_quote(
                quote.get('ask'), formula=formula, mid=mid, stock_bid=stock_bid,
                strike=strike, buffer_multiple=buffer_multiple, buffer=buffer,
                underlying=underlying)
            if limit is None:
                continue
            return limit, quote, source, _parity_applied(
                quote.get('ask'), formula=formula, mid=mid, stock_bid=stock_bid,
                strike=strike, buffer_multiple=buffer_multiple, buffer=buffer)
        return None

    def _price_stc_rung1(self, candidates: List[Tuple[Any, str]], *,
                         formula: str, buffer: Any, underlying: str
                         ) -> Optional[Tuple[float, Dict[str, Any], str]]:
        """``(limit, quote, source)`` for rung 1's screen limit (no floor yet)
        from the first candidate quote that prices, or None."""
        for quote, source in candidates:
            if not isinstance(quote, dict) or not quote:
                continue
            limit = self._stc_limit_from_quote(
                quote.get('bid'), quote.get('ask'), formula, buffer_multiple=1,
                buffer=buffer, floor=None, underlying=underlying)
            if limit is not None:
                return limit, quote, source
        return None

    @staticmethod
    def _apply_rung1_pricing(opportunity: Dict[str, Any], stc_limit: float,
                             quote: Dict[str, Any], source: str) -> None:
        """The S9 sites: rung 1's limit everywhere the ladder reads it, and the
        quote it was priced from as rung 1's PRE-BTC basis (R6-D: always the
        LAST pre-placement read of the new symbol)."""
        opportunity['stc_limit'] = stc_limit
        if opportunity.get('fallback_candidates'):
            opportunity['fallback_candidates'][0]['stc_limit'] = stc_limit
        opportunity['stc_quote_rung1'] = quote
        opportunity['stc_quote_rung1_source'] = source

    def _price_legs_at_execution(self, opportunity: Dict[str, Any]) -> bool:
        """Re-derive both limits from three bounded reads taken NOW (DD-3).

        ``get_option_quote(old)`` + ``get_stock_quote(underlying)`` +
        ``get_option_quote(new)``, each capped at 3.0 s on the quiet data
        plane. The BTC limit comes from the fresh ask
        (``btc_quote_source="execution"``), else from the evaluation-time quote
        (``"evaluation"``); when BOTH are unusable the position is a true skip,
        ``quote_unusable`` (no order exists). A failed or insane stock read
        omits the parity term (``stock_quote_source="none"``). Rung 1's screen
        limit comes from the fresh new-symbol quote ONLY: an empty or
        unpriceable read is ``credit_gone_at_execution``, exactly as main
        skipped on an empty re-check read — no order exists yet, so skipping
        costs nothing, and the chain row is minutes old (review finding F5:
        the pre-BTC screen is never priced off it). The invariant is re-run on
        the buffered pair (``credit_gone_at_execution``; the skip names both
        BTC limits and ``btc_quote_source``). On success the S9 sites are
        replaced. (After the BTC has filled, rung 1 MAY fall back — to the
        pre-BTC read — because the alternative is uncovered shares; see
        ``_rungs``.)

        Returns False after emitting the terminal skip (its reason is on the
        roll context); True to proceed. Places nothing.
        """
        underlying = opportunity['underlying']
        old_symbol = opportunity['old_option_symbol']
        formula = 'imminence' if opportunity.get('imminent') else 'base'
        buffer = self.config.rolling_marketable_buffer_per_share
        earnings_info = opportunity.get('earnings_info', {})

        old_quote = self._pricing_read(self.alpaca.get_option_quote, old_symbol)
        stock_quote = self._pricing_read(self.alpaca.get_stock_quote, underlying)
        new_quote = self._pricing_read(self.alpaca.get_option_quote,
                                       opportunity['new_option_symbol'])

        stock_bid, stock_source = _parity_stock_bid(stock_quote, 'execution')
        btc = self._price_btc(
            [(old_quote, 'execution'), (opportunity.get('btc_quote'), 'evaluation')],
            formula=formula, buffer_multiple=1, stock_bid=stock_bid,
            strike=opportunity['old_strike'], buffer=buffer, underlying=underlying)
        if btc is None:
            _roll_ctx(opportunity)['skip_reason'] = 'quote_unusable'
            self.log_terminal_skip(
                old_symbol, underlying, "quote_unusable",
                current_strike=opportunity['old_strike'],
                target_strike=opportunity['new_strike'],
                evaluated_btc_limit=opportunity.get('btc_limit'),
                pricing_mode=opportunity.get('pricing_mode'),
                **earnings_info, **self._opportunity_quote_set(opportunity))
            return False
        btc_limit, btc_quote, btc_source, parity_applied = btc

        # F5: the fresh read ONLY — never the chain row before any order.
        stc = self._price_stc_rung1(
            [(new_quote, 'execution')],
            formula=formula, buffer=buffer, underlying=underlying)
        stc_limit = stc[0] if stc else None
        if stc_limit is None or (stc_limit - btc_limit) + _CREDIT_EPSILON \
                < opportunity['min_credit_per_share']:
            _roll_ctx(opportunity)['skip_reason'] = 'credit_gone_at_execution'
            self.log_terminal_skip(
                old_symbol, underlying, "credit_gone_at_execution",
                current_strike=opportunity['old_strike'],
                target_strike=opportunity['new_strike'],
                btc_limit=btc_limit, btc_limit_fresh=btc_limit,
                btc_quote_source=btc_source,
                evaluated_btc_limit=opportunity.get('btc_limit'),
                evaluated_stc_limit=opportunity['stc_limit'],
                recheck_stc_limit=stc_limit,
                pricing_mode=opportunity['pricing_mode'],
                **earnings_info, **self._opportunity_quote_set(opportunity))
            return False

        self._apply_rung1_pricing(opportunity, stc_limit, stc[1], stc[2])
        opportunity['btc_limit'] = btc_limit
        opportunity['net_credit_per_contract'] = round((stc_limit - btc_limit) * 100, 2)
        opportunity['btc_pricing'] = {
            'attempt': 0, 'limit': btc_limit, 'quote': btc_quote,
            'quote_source': btc_source, 'stock_bid': stock_bid,
            'stock_quote_source': stock_source,
            'parity_floor_applied': parity_applied, 'formula': formula,
        }
        return True

    # ------------------------------------------------------------------ #
    # FC-120 PR-2 — the buy-to-close leg and its re-price (DD-4)
    # ------------------------------------------------------------------ #
    def _place_and_settle_btc(self, opportunity: Dict[str, Any]
                              ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        """Place, poll, and (after the roller's OWN zero-fill cancel) re-price.

        Returns ``(filled_order, leg)``. ``filled_order`` is the terminal order
        dict with ``filled_qty > 0`` (full or partial), or None. When None,
        ``leg`` is the result dict ``_execute_roll`` returns VERBATIM — the
        terminal (``btc_rejected`` / ``btc_timeout_canceled`` /
        ``btc_disposition_unknown``) has already been emitted, exactly one.
        Otherwise ``leg`` is the attempt context the STO ladder needs.

        ``btc_fill_timeout_seconds`` is the leg's TOTAL poll budget, split over
        ``btc_reprice_attempts + 1`` windows. A window that times out is
        cancelled and SETTLED (two reads minimum). Only a settle that returns
        ``canceled`` with zero fill — the roller's own cancel landing — is
        re-priced: fresh reads, the base formula at ``(n+2) x buffer`` (R6-C),
        never below the prior limit plus one tick (F1), the invariant re-run.
        Every attempt's ``client_order_id`` is salted with ``roll_id`` and the
        attempt index (F1). ``rejected`` is terminal. A terminal zero fill
        from the PRIMARY poll (``expired``; ``canceled`` by another actor) is
        terminal too (R6-B): the roller did not cancel that order and cannot
        know what book it would re-price against — which is also why a replay
        never re-prices. A settle that never resolves is ``unknown``: no second
        order, ever.
        """
        underlying = opportunity['underlying']
        old_symbol = opportunity['old_option_symbol']
        contracts = opportunity['contracts']
        earnings_info = opportunity.get('earnings_info', {})
        attempts = self.config.rolling_btc_reprice_attempts
        window = self.config.rolling_btc_fill_timeout_seconds // (attempts + 1)
        pricing = opportunity['btc_pricing']

        while True:
            n, limit, quote = pricing['attempt'], pricing['limit'], pricing.get('quote')
            # FC-120 PR-1: the quote this limit was priced from, computed at
            # placement so ``quote_age_s`` is its age when the order went out.
            fields = self._placement_fields(
                opportunity, leg='btc', quote=quote, limit=limit,
                strike=opportunity['old_strike'], attempt=n)
            fields.update(_btc_pricing_fields(pricing))
            placed = dict(fields)
            placed.update(current_strike=opportunity['old_strike'],
                          contracts=contracts, limit_price=limit,
                          pricing_mode=opportunity['pricing_mode'])
            log_trade_event(
                logger, event_type="call_roll_btc_placed",
                symbol=old_symbol, underlying=underlying,
                strategy="roll_call", success=True, **placed)

            t0 = time.monotonic()
            result = self.alpaca.place_option_order(
                symbol=old_symbol, qty=contracts,
                side='buy', order_type='limit', limit_price=limit,
                client_order_salt=_client_order_salt(opportunity, 'btc', n))

            def settled(disposition: str, order_id: Optional[str], *,
                        _fields: Dict[str, Any] = fields, _limit: float = limit,
                        **kw: Any) -> None:
                self._log_leg_settled(
                    symbol=old_symbol, underlying=underlying, order_id=order_id,
                    disposition=disposition, placement=_fields, limit=_limit,
                    requested_qty=contracts, **kw)

            if not result or not result.get('success', False):
                error_msg = (result.get('error_message', 'BTC order rejected')
                             if result else 'No result')
                log_error_event(
                    logger, error_type="call_roll_btc_rejected",
                    error_message=error_msg, component="call_roller",
                    recoverable=True, symbol=old_symbol, underlying=underlying,
                    limit_price=limit, contracts=contracts, attempt=n,
                )
                settled('rejected', (result or {}).get('order_id'),
                        elapsed=round(time.monotonic() - t0, 3))
                return None, {'success': False, 'reason': 'btc_rejected',
                              'error': error_msg, 'underlying': underlying}

            order_id = result.get('order_id', '')
            _roll_ctx(opportunity)['btc_order_id'] = order_id
            # FC-120 PR-1 (rulings A/B): the re-read only now that the order is
            # out, bounded; then the previous attempt's deferred settle row.
            fields.update(_reread_fields(
                quote, self._diag_option_quote(old_symbol), 'buy'))
            self._flush_deferred()

            # --- Poll, then cancel-and-VERIFY on timeout ---
            # "Cancel failed because it filled" is a fill, not an error.
            order = self._poll_order_fill(order_id, timeout=window)
            # FC-120 T7: snapshot BEFORE the settle re-enters the poll.
            polls = self._last_poll_reads
            settle_polls: Optional[int] = None
            timed_out = order is None
            if timed_out:
                order = self._cancel_and_settle(order_id)
                settle_polls = self._last_poll_reads
            elapsed = round(time.monotonic() - t0, 3)
            timing = dict(elapsed=elapsed, polls=polls, settle_polls=settle_polls)
            if timed_out and order is None:
                settled('unknown', order_id, **timing)
                # We do NOT know whether contracts were bought: never sell a
                # call against a short position whose size is unknown. Page.
                return None, self._unknown_disposition(
                    leg='btc', order_id=order_id, symbol=old_symbol,
                    underlying=underlying, earnings_info=earnings_info,
                    detail=("BTC cancel did not settle to a terminal status "
                            "within the bound; the order may still be working"))

            filled_qty = int(_as_float(order.get('filled_qty')))
            if filled_qty > 0:
                return order, self._btc_filled(
                    opportunity, pricing, order, order_id, fields, filled_qty,
                    settled, timing)

            status = order.get('status')
            if status == 'rejected':
                # L-2: a broker rejection after placement is a REJECTION, never
                # re-priced (R2): it is the broker's verdict on the order.
                log_error_event(
                    logger, error_type="call_roll_btc_rejected",
                    error_message=f"BTC order {order_id} rejected after placement",
                    component="call_roller", recoverable=True,
                    symbol=old_symbol, underlying=underlying,
                    order_id=order_id, limit_price=limit,
                    contracts=contracts, rejected_after_placement=True, attempt=n,
                )
                settled('rejected', order_id, order=order, **timing)
                return None, {'success': False, 'reason': 'btc_rejected',
                              'order_id': order_id, 'underlying': underlying}

            disposition = _zero_fill_disposition(status, timed_out)
            refusal: Optional[Dict[str, Any]] = None
            prefetched: Any = _NOT_PREFETCHED
            if timed_out and status == 'canceled' and n < attempts:
                nxt, refusal, prefetched = self._reprice_btc(opportunity, pricing)
                if nxt is not None:
                    self._log_btc_repriced(opportunity, pricing, nxt, order_id)
                    self._defer_btc_settled(settled, order_id, order, disposition,
                                            quote, prefetched, timing,
                                            opportunity)
                    pricing = opportunity['btc_pricing'] = nxt
                    continue

            # The terminal goes out FIRST (ruling A); the post-settle quote is
            # read after it and rides on the later leg_settled row alone.
            timeout_payload = _safe_merge(fields, dict(
                order_id=order_id, order_status=_str_or_none(status),
                disposition=disposition, limit_price=limit, contracts=contracts,
                leg_elapsed_s=elapsed,
                order_submitted_at=_iso(order.get('submitted_at')),
                polls=polls, settle_polls=settle_polls, attempts=n + 1))
            if refusal:
                timeout_payload.update(refusal)
            timeout_payload.update(earnings_info)
            log_trade_event(
                logger, event_type="call_roll_btc_timeout_canceled",
                symbol=old_symbol, underlying=underlying,
                strategy="roll_call", success=False, **timeout_payload)
            self._defer_btc_settled(settled, order_id, order, disposition, quote,
                                    prefetched, timing, opportunity)
            return None, {'success': False, 'reason': 'btc_timeout_canceled',
                          'order_id': order_id, 'underlying': underlying}

    def _btc_filled(self, opportunity: Dict[str, Any], pricing: Dict[str, Any],
                    order: Dict[str, Any], order_id: str, fields: Dict[str, Any],
                    filled_qty: int, settled: Callable[..., None],
                    timing: Dict[str, Any]) -> Dict[str, Any]:
        """A buy-to-close attempt filled (full or partial): its fill event and
        settled row, and the attempt context the STO ladder reads."""
        limit = pricing['limit']
        filled_price = _as_float(order.get('filled_avg_price')) or limit
        # FC-120 PR-1 (T-LOW-8): the broker's price, or the limit standing in?
        price_source = _fill_price_source(order)
        payload = dict(fields)
        payload.update(self._fill_fields(
            limit=limit, filled_price=filled_price, side='buy', order=order,
            price_source=price_source, **timing))
        payload.update(order_id=order_id, filled_qty=filled_qty,
                       filled_price=filled_price,
                       requested_qty=opportunity['contracts'])
        log_trade_event(
            logger, event_type="call_roll_btc_filled",
            symbol=opportunity['old_option_symbol'],
            underlying=opportunity['underlying'],
            strategy="roll_call", success=True, **payload)
        settled('filled' if filled_qty >= opportunity['contracts'] else 'partial',
                order_id, filled_qty=filled_qty, filled_price=filled_price,
                order=order, price_source=price_source, **timing)
        return {'attempt': pricing['attempt'], 'btc_order_id': order_id,
                'btc_filled_qty': filled_qty, 'btc_filled_price': filled_price,
                'btc_limit': limit}

    def _defer_btc_settled(self, settled: Callable[..., None], order_id: str,
                           order: Dict[str, Any], disposition: str,
                           quote: Any, prefetched: Any,
                           timing: Dict[str, Any],
                           opportunity: Dict[str, Any]) -> None:
        """Queue a zero-fill BTC attempt's post-settle quote + settled row
        (ruling A: after the next placement, or after the terminal). A re-price
        read IS this attempt's cancel-time option quote (one read, two uses),
        so with ``prefetched`` only the IEX stock quote is read here."""
        old_symbol = opportunity['old_option_symbol']
        underlying = opportunity['underlying']

        def settle_row() -> None:
            cancel = self._post_settle_quote(old_symbol, underlying, quote, 'buy',
                                             prefetched=prefetched)
            settled(disposition, order_id, order=order, cancel=cancel, **timing)
        self._deferred.append(settle_row)

    def _reprice_btc(self, opportunity: Dict[str, Any], pricing: Dict[str, Any]
                     ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Any]:
        """Re-price after the roller's own zero-fill cancel (DD-4 step 5).

        Fresh bounded reads of the old option, the stock and the new option.
        The BTC is priced by the BASE formula at ``(n+2) x buffer`` whatever the
        evaluation's mode (R6-C), from the fresh ask (``"reprice"``) or, if that
        read failed, the prior attempt's ask (``"prior_basis"``) — and it must
        STRICTLY improve on the limit it replaces (review finding F1):
        ``max(formula, prior_limit + one tick)``, the tick decided from the
        unsnapped value (``_strict_step_above``). A falling ask (or a zero
        buffer) would otherwise re-place the same price — one that just rested
        a full window — and, before the ``client_order_id`` salt, Alpaca
        refused it as a duplicate. ``improvement_floor_applied`` says the step,
        not the formula, set the limit (``parity_floor_applied`` is then
        false). Rung 1's limit is re-derived from the fresh new-symbol read and
        replaces the S9 sites (R6-D — it becomes rung 1's pre-BTC basis); the
        invariant is re-run on the stepped pair. Returns
        ``(next_pricing, None, prefetched)`` to place, or
        ``(None, refusal, prefetched)`` with ``reprice_skipped_reason`` in
        ``{quote_unusable, credit_gone}`` — the leg's terminal is then
        ``call_roll_btc_timeout_canceled``, never a skip (R6-E). ``prefetched``
        is the fresh old-option read (None if it failed): the canceled
        attempt's cancel-time quote.
        """
        underlying = opportunity['underlying']
        n_next = pricing['attempt'] + 1
        buffer = self.config.rolling_marketable_buffer_per_share
        old_quote = self._pricing_read(self.alpaca.get_option_quote,
                                       opportunity['old_option_symbol'])
        stock_quote = self._pricing_read(self.alpaca.get_stock_quote, underlying)
        new_quote = self._pricing_read(self.alpaca.get_option_quote,
                                       opportunity['new_option_symbol'])
        stock_bid, stock_source = _parity_stock_bid(stock_quote, 'reprice')
        btc = self._price_btc(
            [(old_quote, 'reprice'), (pricing.get('quote'), 'prior_basis')],
            formula='base', buffer_multiple=n_next + 1, stock_bid=stock_bid,
            strike=opportunity['old_strike'], buffer=buffer, underlying=underlying)
        if btc is None:
            return None, {'reprice_skipped_reason': 'quote_unusable'}, old_quote
        btc_limit, btc_quote, btc_source, parity_applied = btc
        # F1: never the same (or a lower) price than the attempt it replaces.
        step = _strict_step_above(pricing.get('limit'), underlying)
        improved = step is not None and step > btc_limit
        if improved:
            btc_limit, parity_applied = step, False

        formula = 'imminence' if opportunity.get('imminent') else 'base'
        stc = self._price_stc_rung1(
            [(new_quote, 'reprice'),
             (opportunity.get('stc_quote_rung1'),
              opportunity.get('stc_quote_rung1_source') or 'execution')],
            formula=formula, buffer=buffer, underlying=underlying)
        stc_limit = stc[0] if stc else None
        if stc_limit is None or (stc_limit - btc_limit) + _CREDIT_EPSILON \
                < opportunity['min_credit_per_share']:
            return None, {'reprice_skipped_reason': 'credit_gone',
                          'btc_limit_fresh': btc_limit,
                          'stc_limit_fresh': stc_limit,
                          'improvement_floor_applied': improved}, old_quote

        self._apply_rung1_pricing(opportunity, stc_limit, stc[1], stc[2])
        opportunity['btc_limit'] = btc_limit
        return {
            'attempt': n_next, 'limit': btc_limit, 'quote': btc_quote,
            'quote_source': btc_source, 'stock_bid': stock_bid,
            'stock_quote_source': stock_source,
            'parity_floor_applied': parity_applied, 'formula': 'base',
            'improvement_floor_applied': improved,
            'stc_limit': stc_limit,
        }, None, old_quote

    def _log_btc_repriced(self, opportunity: Dict[str, Any],
                          prior: Dict[str, Any], nxt: Dict[str, Any],
                          prior_order_id: str) -> None:
        """``call_roll_btc_repriced`` — per attempt, informational, never a
        terminal. Total: a logging failure leaves one breadcrumb."""
        try:
            pq = prior.get('quote') if isinstance(prior.get('quote'), dict) else {}
            nq = nxt.get('quote') if isinstance(nxt.get('quote'), dict) else {}
            log_trade_event(
                logger, event_type="call_roll_btc_repriced",
                symbol=opportunity['old_option_symbol'],
                underlying=opportunity['underlying'],
                strategy="roll_call", success=True,
                attempt=_int_or_none(nxt.get('attempt')),
                prior_limit=_pos(prior.get('limit')),
                new_limit=_pos(nxt.get('limit')),
                prior_order_id=_str_or_none(prior_order_id),
                prior_quote_bid=_pos(pq.get('bid')),
                prior_quote_ask=_pos(pq.get('ask')),
                prior_quote_ts=_iso(pq.get('timestamp')),
                quote_bid=_pos(nq.get('bid')), quote_ask=_pos(nq.get('ask')),
                quote_ts=_iso(nq.get('timestamp')),
                quote_age_s=_age_s(nq.get('timestamp')),
                stock_bid=_pos(nxt.get('stock_bid')),
                stc_limit=_pos(nxt.get('stc_limit')),
                pricing_mode=_str_or_none(opportunity.get('pricing_mode')),
                roll_id=_str_or_none(_roll_ctx(opportunity).get('roll_id')),
                **_btc_pricing_fields(nxt))
        except Exception as exc:
            _instrumentation_failed('btc_repriced', exc)

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

        FC-120 PR-2 (DD-3/DD-4): every rung polls ``stc_rung_timeout_seconds``
        (30 s), never the BTC's window. The ladder is rung 1, then
        ``stc_escalation_rungs`` escalation rungs (shipped 0), then the floor,
        then the fallbacks. An escalation rung is placed ONLY after the
        previous primary-ladder rung ended in the roller's own cancel-settle
        with zero fill (``canceled``) or a synchronous rejection — a terminal
        zero fill from the PRIMARY poll goes straight to the floor (R6-B).
        """
        underlying = opportunity['underlying']
        ladder = self._new_ladder(opportunity, btc_filled_price)
        # (order_id, symbol, strike, limit, leg_ctx) of a rung that timed out
        # and is still working: the NEXT step cancels-and-settles it first.
        live: Optional[Tuple[str, str, float, float, Dict[str, Any]]] = None

        for symbol, limit, new_strike, rung_quote in self._rungs(
                opportunity, btc_filled_price, ladder):
            if live is not None:
                pending, live = live, None
                stop = self._settle_or_stop(pending, underlying)
                if stop is not None:
                    return stop

            if rung_quote.get('rung_kind') == 'floor' and rung_quote.get('rung') != 1:
                # The escalation rungs sit between rung 1 and the floor. With
                # the shipped E = 0 none is placed and the floor follows rung 1
                # with NO read in between (R6-Q).
                if ladder['E'] > 0:
                    outcome = self._escalate(opportunity, qty, ladder)
                    if outcome is not None:
                        return outcome
                # The floor carries the quote (and re-read) of the last rung
                # that had one, and takes the next position on the ladder.
                rung_quote['rung'] = 2 + ladder['escalations_placed']
                rung_quote['quote'] = ladder['reuse_quote']
                rung_quote['reused_from_rung'] = ladder['reuse_rung']

            status, payload = self._run_rung(opportunity, qty, symbol, limit,
                                             new_strike, rung_quote, ladder)
            if status == 'filled':
                return payload
            if status == 'live':
                live = payload

        if live is not None:
            stop = self._settle_or_stop(live, underlying)
            if stop is not None:
                return stop
        return None

    def _settle_or_stop(self, live: Tuple[str, str, float, float, Dict[str, Any]],
                        underlying: str) -> Optional[Dict[str, Any]]:
        """Cancel-and-settle a working rung. Returns the ladder's result when
        it must stop — the rung's fill (the cancel lost the race) or the
        unknown-disposition marker — and None when it settled zero-fill."""
        resolved, settled = self._settle_live_rung(live, underlying)
        if resolved:
            return resolved
        if not settled:
            return {'success': False, 'unknown_disposition': True,
                    'order_id': live[0], 'symbol': live[1]}
        return None

    def _escalate(self, opportunity: Dict[str, Any], qty: int,
                  ladder: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Place escalation rungs 2 ... 1+E between rung 1 and the floor
        (``stc_escalation_rungs > 0`` only; shipped 0).

        Each one: the previous primary-ladder rung must have ended in the
        roller's OWN cancel-settle with zero fill, or a synchronous rejection
        (R6-B / R6-F) — else the ladder goes to the floor. Then ONE bounded
        basis read, which doubles as the previous rung's cancel-time quote;
        the base formula at ``(n+1) x buffer`` off ``min(basis, fresh_bid)``
        (R6-C); a computed limit not strictly above the floor IS the floor —
        escalation stops and the floor follows. Returns the ladder's result if
        it must stop (a fill or an unknown disposition), else None.
        """
        underlying = opportunity['underlying']
        primary_symbol = opportunity['new_option_symbol']
        new_strike = opportunity['new_strike']
        for n in range(1, ladder['E'] + 1):
            prior = ladder.get('prior') or {}
            if not prior.get('escalation_ok'):
                return None
            fresh = self._pricing_read(self.alpaca.get_option_quote, primary_symbol)
            prior_ctx = prior.get('ctx')
            if isinstance(prior_ctx, dict):
                prior_ctx['prefetched_cancel'] = fresh    # one read, two uses
            fresh_bid = _pos((fresh or {}).get('bid'))
            limit, rung_quote = self._escalation_rung(
                ladder['basis_bid'], n, ladder['floor'], fresh_bid,
                buffer=ladder['buffer'], underlying=underlying,
                fresh_quote=fresh, prior_quote=ladder['basis_quote'])
            if limit is None:
                return None
            rung_quote['rung'] = 1 + n
            ladder['escalations_placed'] = n
            ladder['basis_bid'] = rung_quote['basis_bid']
            ladder['basis_quote'] = rung_quote['quote']
            ladder['reuse_quote'], ladder['reuse_rung'] = rung_quote['quote'], 1 + n
            status, payload = self._run_rung(opportunity, qty, primary_symbol,
                                             limit, new_strike, rung_quote, ladder)
            if status == 'filled':
                return payload
            if status == 'live':
                stop = self._settle_or_stop(payload, underlying)
                if stop is not None:
                    return stop
        return None

    def _escalation_rung(self, basis: Optional[float], n: int, floor: float,
                         fresh_bid: Optional[float], *, buffer: Any,
                         underlying: str, fresh_quote: Any = None,
                         prior_quote: Any = None
                         ) -> Tuple[Optional[float], Dict[str, Any]]:
        """Escalation rung ``1+n``: ``max(snap_down(basis' - (n+1) x buffer),
        floor)`` with ``basis' = min(basis, fresh_bid)`` (``fresh_bid`` None →
        ``basis``, source ``prior_basis``). The BASE formula in both pricing
        modes (R6-C). Returns ``(None, rung_quote)`` when the limit is not
        strictly above the floor — that rung IS the floor."""
        if fresh_bid is not None and basis is not None:
            new_basis, source = min(basis, fresh_bid), 'fresh'
        elif fresh_bid is not None:
            new_basis, source = fresh_bid, 'fresh'
        else:
            new_basis, source = basis, 'prior_basis'
        quote = fresh_quote if fresh_bid is not None else prior_quote
        rung_quote = {'rung_kind': 'escalation', 'escalation_index': n,
                      'quote': quote if isinstance(quote, dict) else None,
                      'basis_bid': new_basis, 'stc_quote_source': source,
                      'limit_formula': 'base'}
        limit = self._stc_limit_from_quote(
            new_basis, None, 'base', buffer_multiple=n + 1, buffer=buffer,
            floor=floor, underlying=underlying)
        if limit is None or limit <= floor:
            return None, rung_quote
        return limit, rung_quote

    def _new_ladder(self, opportunity: Dict[str, Any],
                    btc_filled_price: float) -> Dict[str, Any]:
        """The STO ladder's state for one position. ``floor`` is
        ``snap_up(btc_fill + min_credit)`` — rounded UP (down would breach the
        invariant); at 0.20 $/contract it is the BTC fill plus one tick."""
        underlying = opportunity.get('underlying', '')
        raw = _dec(btc_filled_price) + _dec(opportunity['min_credit_per_share'])
        _warn_if_unverified(underlying)
        floor = float(snap_limit(raw, tick_size(raw, underlying), "up"))
        return {
            'E': self.config.rolling_stc_escalation_rungs,
            'floor': floor,
            'formula': 'imminence' if opportunity.get('imminent') else 'base',
            'buffer': self.config.rolling_marketable_buffer_per_share,
            'primary_limit': None, 'last_primary_limit': None,
            'primary_disposition': None, 'basis_bid': None, 'basis_quote': None,
            'reuse_quote': None, 'reuse_rung': 1, 'escalations_placed': 0,
            'prior': None, 'rereads': {},
        }

    def _rungs(self, opportunity: Dict[str, Any], btc_filled_price: float,
               ladder: Optional[Dict[str, Any]] = None
               ) -> Iterator[Tuple[str, float, float, Dict[str, Any]]]:
        """Yield ``(symbol, limit, strike, rung_quote)`` for rung 1, the floor
        and the fallbacks, each priced when reached (the escalation rungs are
        placed between rung 1 and the floor by ``_attempt_stc``).

        Rung 1 — ``max(snap_down(basis - buffer), floor)`` with ``basis =
                 min(pre_btc_bid, fresh_bid)``: the pre-BTC bid is the LAST
                 pre-placement read of the new symbol (execute time, or the
                 last BTC re-price — R6-D); ``fresh_bid`` is ONE bounded pricing
                 read taken right after the BTC fill. The lower of two recent
                 indicative reads is the safer estimate of the true bid, and a
                 lower limit costs nothing on a fill (fills land at the NBBO).
                 A rung-1 limit not above the floor IS the floor: it is logged
                 ``rung=1, rung_kind="floor"`` and no second floor follows.
        Floor  — ``snap_up(btc_fill + min_credit)``, quote-free, yielded only
                 when strictly below the last primary-ladder limit computed
                 (placed or synchronously rejected). It reuses the quote and
                 re-read of the last rung that had one.
        Fallbacks — up to ``fallback_strike_attempts`` further candidates from
                 the stored legal list, each re-quoted (bounded), priced by the
                 pricing mode's formula WITH the buffer, re-validated through
                 ``validate_roll`` and re-tested against the invariant from the
                 ACTUAL BTC fill. Never re-queried from the chain (M-8).

        ``rung`` (FC-120 PR-1) is the ladder position: 1 primary, 2...1+E the
        escalations, the floor next, the fallbacks after it (numbered as if
        the floor took its slot, so with E = 0 they are PR-1's 3, 4).
        """
        if ladder is None:
            ladder = self._new_ladder(opportunity, btc_filled_price)
        underlying = opportunity['underlying']
        primary_symbol = opportunity['new_option_symbol']
        new_strike = opportunity['new_strike']
        floor, formula, buffer = ladder['floor'], ladder['formula'], ladder['buffer']
        candidate_row = opportunity.get('candidate')

        fresh = self._pricing_read(self.alpaca.get_option_quote, primary_symbol)
        basis = _rung1_basis(opportunity.get('stc_quote_rung1'),
                             opportunity.get('stc_quote_rung1_source'),
                             fresh, formula)
        limit = self._stc_limit_from_quote(
            basis['bid'], basis['ask'], formula, buffer_multiple=1,
            buffer=buffer, floor=floor, underlying=underlying)
        if limit is None:
            limit = floor        # no usable basis: the invariant's own price
        at_floor = limit <= floor
        ladder.update(primary_limit=limit, last_primary_limit=limit,
                      basis_bid=basis['basis_bid'], basis_quote=basis['quote'],
                      reuse_quote=basis['quote'], reuse_rung=1)
        yield (primary_symbol, limit, new_strike, {
            'rung': 1, 'rung_kind': 'floor' if at_floor else 'primary',
            'escalation_index': None if at_floor else 0,
            'quote': basis['quote'], 'basis_bid': basis['basis_bid'],
            'stc_quote_source': basis['source'],
            'pre_btc_bid': basis['pre_btc_bid'],
            'pre_btc_quote_source': basis['pre_btc_source'],
            'limit_formula': formula,
            'chain_stamp': (candidate_row.get('quote_timestamp')
                            if isinstance(candidate_row, dict) else None)})

        if not at_floor and 0 < floor < (ladder['last_primary_limit'] or 0):
            yield (primary_symbol, floor, new_strike, {
                'rung': 2, 'rung_kind': 'floor', 'escalation_index': None,
                'quote': ladder['reuse_quote'], 'reused_from_rung': 1,
                'basis_bid': None, 'stc_quote_source': None,
                'limit_formula': None})

        attempts = self.config.rolling_fallback_strike_attempts
        min_credit = opportunity['min_credit_per_share']
        for index, entry in enumerate(
                opportunity['fallback_candidates'][1:1 + attempts]):
            symbol = entry['new_option_symbol']
            quote = self._pricing_read(self.alpaca.get_option_quote, symbol) or {}
            limit = self._stc_limit_from_quote(
                quote.get('bid'), quote.get('ask'), formula, buffer_multiple=1,
                buffer=buffer, floor=None, underlying=underlying)
            if limit is None:
                continue

            valid, _reason = self.risk_manager.validate_roll(
                entry['candidate'], opportunity['old_strike'],
                opportunity['cost_basis_per_share'], opportunity['max_expiry'])
            if not valid:
                continue

            # The invariant against what the BTC ACTUALLY cost, not its limit
            # — and never below the floor (equivalent on a tick grid; stated).
            if (limit - btc_filled_price) + _CREDIT_EPSILON < min_credit \
                    or limit + _CREDIT_EPSILON < floor:
                continue

            yield (symbol, limit, entry['new_strike'], {
                'rung': 3 + ladder['escalations_placed'] + index,
                'rung_kind': 'fallback', 'escalation_index': None,
                'quote': quote, 'basis_bid': _pos(quote.get('bid')),
                'stc_quote_source': 'fresh', 'limit_formula': formula})

    def _run_rung(self, opportunity: Dict[str, Any], qty: int, symbol: str,
                  limit: float, new_strike: float, rung_quote: Dict[str, Any],
                  ladder: Dict[str, Any]) -> Tuple[str, Any]:
        """Place ONE rung and poll it ``stc_rung_timeout_seconds``. Returns:

        - ``('filled', result)`` — it filled (full or partial) on the poll;
        - ``('live', live_tuple)`` — it timed out still working: the caller
          cancels-and-settles it before ANY other order action;
        - ``('rejected', None)`` — refused synchronously; nothing is live;
        - ``('terminal_zero', None)`` — the PRIMARY poll saw a terminal zero
          fill (``expired`` / canceled by another actor / rejected): nothing is
          live, and no escalation rung may follow it (R6-B).
        """
        underlying = opportunity['underlying']
        rung = rung_quote.get('rung')
        prior = ladder.get('prior')
        fields = self._placement_fields(
            opportunity, leg='stc', quote=rung_quote.get('quote'),
            limit=limit, strike=new_strike, rung=rung,
            reused_from_rung=rung_quote.get('reused_from_rung'),
            chain_stamp=(rung_quote.get('chain_stamp') if rung == 1 else None))
        fields.update(_stc_ladder_fields(rung_quote, ladder, prior))
        ctx: Dict[str, Any] = {
            'placement': fields, 'limit': limit, 'quote': rung_quote.get('quote'),
            'requested_qty': qty, 'rung': rung, 't0': time.monotonic(),
            'polls': None, 'ladder': ladder,
            'primary_limit': ladder.get('primary_limit'),
            'prior_rung_limit': (prior or {}).get('limit'),
            'prior_rung_disposition': (prior or {}).get('disposition'),
        }
        if rung_quote.get('rung_kind') in ('primary', 'escalation'):
            ladder['last_primary_limit'] = limit   # placed or sync-rejected
        order_id = self._place_stc(
            symbol, underlying, qty, limit, rung=rung, quote_fields=fields,
            pricing_mode=opportunity.get('pricing_mode'),
            client_order_salt=_client_order_salt(opportunity, 'stc', rung))
        if order_id is None:
            self._log_stc_settled(ctx, symbol, underlying, None, 'rejected')
            _record_prior(ladder, rung, limit, 'rejected', escalation_ok=True)
            return 'rejected', None

        # FC-120 PR-1 (ruling A): only now that this rung is OUT — its own
        # re-read (the floor reuses the quote and re-read of the rung it
        # carries), then the previous rungs' post-settle work. Both bounded.
        reused = rung_quote.get('reused_from_rung')
        if reused:
            fields.update(ladder['rereads'].get(reused)
                          or {k: None for k in _REREAD_KEYS})
        else:
            fields.update(_reread_fields(
                rung_quote.get('quote'), self._diag_option_quote(symbol), 'sell'))
            ladder['rereads'][rung] = {k: fields.get(k) for k in _REREAD_KEYS}
        self._flush_deferred()

        order = self._poll_order_fill(
            order_id, timeout=self.config.rolling_stc_rung_timeout_seconds)
        ctx['polls'] = self._last_poll_reads
        if order is None:
            # Timed out with the order still working — carry it forward so the
            # NEXT step cancels-and-verifies before placing anything.
            return 'live', (order_id, symbol, new_strike, limit, ctx)
        ctx['elapsed'] = round(time.monotonic() - ctx['t0'], 3)

        filled_qty = int(_as_float(order.get('filled_qty')))
        if filled_qty > 0:
            return 'filled', self._stc_success(order_id, symbol, underlying,
                                               new_strike, filled_qty, order,
                                               limit, ctx=ctx)
        # Terminal with zero fill on the PRIMARY poll: nothing is live, so the
        # next (non-escalation) rung may be placed directly. The pre-existing
        # event first, fields in hand (ruling A); the post-settle read waits.
        status = order.get('status')
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
        disposition = _zero_fill_disposition(status, False)
        self._defer_stc_settled(ctx, symbol, underlying, order_id, disposition,
                                order)
        _record_prior(ladder, rung, limit, disposition, escalation_ok=False,
                      ctx=ctx)
        return 'terminal_zero', None

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
            status = order.get('status')
            disposition = _zero_fill_disposition(status, True)
            payload = _safe_merge(ctx['placement'], dict(
                order_id=order_id, order_status=_str_or_none(status),
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
            ladder = ctx.get('ladder')
            if isinstance(ladder, dict):
                # Only the roller's OWN cancel landing (``canceled``) may be
                # followed by an escalation rung (R6-B).
                _record_prior(ladder, ctx.get('rung'), limit_price, disposition,
                              escalation_ok=(status == 'canceled'), ctx=ctx)
        return None, True

    def _defer_stc_settled(self, ctx: Dict[str, Any], symbol: str,
                           underlying: str, order_id: Optional[str],
                           disposition: str, order: Dict[str, Any]) -> None:
        """Queue a zero-fill rung's post-settle quote + leg_settled row.

        Ruling A: it runs right after the NEXT rung is placed, or after the
        position's terminal when the ladder ends — never between this rung's
        settle and the next order action. When an escalation rung's basis read
        followed this rung's settle, that read IS this rung's cancel-time
        option quote (``ctx['prefetched_cancel']``, read when the row runs):
        then only the IEX stock quote is read here."""
        def settle_row() -> None:
            cancel = self._post_settle_quote(
                symbol, underlying, ctx.get('quote'), 'sell',
                prefetched=ctx.get('prefetched_cancel', _NOT_PREFETCHED))
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
                   pricing_mode: Optional[str] = None,
                   client_order_salt: Optional[str] = None) -> Optional[str]:
        """Place one STO rung. Returns the order id, or None if it was refused.
        ``client_order_salt`` keys this rung's ``client_order_id`` to the roll
        and the rung (F1), so no two rungs of one roll can collide."""
        payload = dict(quote_fields or {})
        payload.update(contracts=contracts, limit_price=limit_price,
                       leg='stc', rung=rung, pricing_mode=pricing_mode)
        log_trade_event(
            logger, event_type="call_roll_stc_placed",
            symbol=symbol, underlying=underlying,
            strategy="roll_call", success=True, **payload)

        result = self.alpaca.place_option_order(
            symbol=symbol, qty=contracts,
            side='sell', order_type='limit', limit_price=limit_price,
            client_order_salt=client_order_salt)

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
            ctx['extra'] = _miss_offsets(ctx, filled_price, source)
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
        """The STO rung-1 quote set for the dry-run event, every key prefixed
        ``stc_``. No read.

        FC-120 PR-2: the dry run returns AFTER execute-time pricing, so this is
        the quote ``stc_limit`` was actually priced from — the execute-time
        read of the new symbol (``stc_quote_source="execution"``); since F5 an
        unusable read skips before the dry run is reached, so the chain-row
        branch below is defensive only. PR-1, which priced nothing at execute
        time, used the chain row always."""
        try:
            quote = opportunity.get('stc_quote_rung1')
            source = opportunity.get('stc_quote_rung1_source')
            if not isinstance(quote, dict) or not quote:
                quote, source = _chain_row_quote(opportunity) or {}, 'chain'
            fields = _quote_fields(
                quote, opportunity.get('stc_limit'), 'sell',
                opportunity.get('underlying', ''), opportunity.get('new_strike'),
                opportunity.get('stock_bid'))
            # The chain call passes no feed: a chain row's feed is null.
            if source == 'chain':
                fields['quote_feed'] = None
            fields['quote_source'] = _str_or_none(source)
            return {f'stc_{k}': v for k, v in fields.items()}
        except Exception as exc:
            _instrumentation_failed('dry_run_stc_fields', exc)
            return {}

    def _placement_fields(self, opportunity: Dict[str, Any], *, leg: str,
                          quote: Optional[Dict[str, Any]],
                          limit: Any, strike: Any,
                          rung: Optional[int] = None,
                          reused_from_rung: Optional[int] = None,
                          chain_stamp: Any = None,
                          attempt: int = 0) -> Dict[str, Any]:
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
            # 0-based; PR-2 re-prices up to ``btc_reprice_attempts`` times.
            fields['attempt'] = _int_or_none(attempt)
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
                           side: str, *, prefetched: Any = _NOT_PREFETCHED
                           ) -> Dict[str, Any]:
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

        ``prefetched`` (FC-120 PR-2): the option quote a re-price or an
        escalation basis read already took right after this leg's settle — one
        read, two uses — so no second option read is issued; ``None`` there
        means that read failed. Only the IEX stock quote is read here then.
        """
        out = dict(_EMPTY_CANCEL_FIELDS)
        try:
            if prefetched is _NOT_PREFETCHED:
                fresh, why = _bounded_read(self.alpaca.get_option_quote, symbol)
            else:
                fresh = prefetched if isinstance(prefetched, dict) and prefetched \
                    else None
                why = 'ok' if fresh is not None else 'prefetch_failed'
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
    def _poll_order_fill(self, order_id: str, timeout: Optional[int] = None,
                         *, min_reads: int = 1) -> Optional[Dict[str, Any]]:
        """Poll until the order reaches a terminal status, or the timeout.

        FC-120 PR-2 (FC-113 (b)): the window is a MONOTONIC DEADLINE, not a sum
        of sleeps, so ``get_order_by_id`` round-trips count against it — a 30 s
        window is 30 s of wall clock plus one trailing read, whatever the RTT.
        Do-while: read FIRST, return on a terminal status, then stop only once
        at least ``min_reads`` reads were issued AND the deadline has passed,
        else ``sleep(min(5, remaining))``. ``timeout=0, min_reads=1`` is
        exactly one read and no sleep. ``_cancel_and_settle`` passes
        ``min_reads=2`` so a settle observes the order at least twice however
        slow the reads are (the window is a floor, not a cap — R6-I).

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
        deadline = time.monotonic() + timeout
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
            remaining = deadline - time.monotonic()
            if self._last_poll_reads >= min_reads and remaining <= 0:
                return None
            wait = min(_POLL_INTERVAL_SECONDS, max(0.0, remaining))
            if wait > 0:
                time.sleep(wait)

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
            order_id, timeout=_CANCEL_SETTLE_TIMEOUT_SECONDS,
            min_reads=_CANCEL_SETTLE_MIN_READS)

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
    the EVALUATION-time IEX stock bid (its stamp is ``stock_bid_ts``) — a BTC
    limit below it is one no NBBO ask could match. FC-120 PR-2 ALSO reads the
    stock quote before each BTC placement (execute time, and each re-price)
    to price the parity term, so a BTC row carries two stock bids (review
    finding F7): ``stock_bid`` — this evaluation-time one, from
    ``_stock_fields`` — and ``parity_stock_bid``, the execute-time / re-price
    bid, from ``_btc_pricing_fields``. They are different reads at different
    times; neither is a re-read of the other.

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
    """The EVALUATION-time IEX stock quote, its age computed NOW from its own
    stored broker stamp — the same time base as ``quote_age_s``. On a BTC row
    this ``stock_bid`` is NOT the bid that priced the parity term: that one is
    ``parity_stock_bid`` (execute-time or re-price read; ``_btc_pricing_fields``
    — F7)."""
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


# --------------------------------------------------------------------------- #
# FC-120 PR-2 — pricing helpers (docs/plans/fc-120.md DD-3). Exact decimals via
# ``_dec`` throughout: never ``Decimal(float)``.
# --------------------------------------------------------------------------- #

def _parity_floor(stock_bid: Any, strike: Any) -> Optional[Decimal]:
    """``stock_bid - strike + 0.01``: a LOWER BOUND on the buy limit — never
    below intrinsic at the IEX stock bid — not a marketability guarantee
    (review finding F7). A call's true ask cannot sit below intrinsic, so a
    limit under parity could never fill; but the true ask carries time value
    above parity, so a limit AT parity + 0.01 is marketable only when the
    contract trades at (or within a cent of) intrinsic. It binds when the
    indicative ask is quoted below parity — the modified-quote failure a
    buffer cannot bound. None when either input is absent (the term is
    omitted)."""
    stock, k = _pos(stock_bid), _pos(strike)
    if stock is None or k is None:
        return None
    return _dec(stock) - _dec(k) + Decimal("0.01")


def _btc_raw(ask: Any, *, formula: str, mid: Any, stock_bid: Any, strike: Any,
             buffer_multiple: int, buffer: Any) -> Optional[Tuple[Decimal, bool]]:
    """The unsnapped buy-to-close price and whether the PARITY term (rather
    than ``ask + buffer`` / ``mid + pad``) was the binding operand."""
    if formula == 'imminence':
        m = _pos(mid)
        if m is None:
            return None
        base = _dec(m) + _dec(_IMMINENCE_PAD)
    else:
        a = _pos(ask)
        if a is None:
            return None
        base = _dec(a) + Decimal(str(buffer)) * buffer_multiple
    parity = _parity_floor(stock_bid, strike)
    if parity is not None and parity > base:
        return parity, True
    return base, False


def _strict_step_above(prior_limit: Any, underlying: str) -> Optional[float]:
    """The lowest legal buy limit STRICTLY above ``prior_limit`` (review
    finding F1 — a BTC re-price must strictly improve): the prior limit plus
    one tick of its own grid, snapped UP on the grid of that unsnapped value
    (DD-3's rule: the tick is decided from the unsnapped value), so on a
    penny-program root 1.33 -> 1.34, 2.99 -> 3.00 and 3.00 -> 3.05. None when
    the prior limit is unusable (the formula alone then prices)."""
    p = _pos(prior_limit)
    if p is None:
        return None
    raw = _dec(p) + tick_size(_dec(p), underlying)
    return float(snap_limit(raw, tick_size(raw, underlying), "up"))


def _parity_applied(ask: Any, **kw: Any) -> Optional[bool]:
    """``parity_floor_applied`` for the log; None when unpriceable. Total."""
    try:
        raw = _btc_raw(ask, **kw)
    except Exception:
        return None
    return None if raw is None else raw[1]


def _mid(quote: Any) -> Optional[float]:
    """The exact two-sided mid of a quote, or None."""
    q = quote if isinstance(quote, dict) else {}
    bid, ask = _pos(q.get('bid')), _pos(q.get('ask'))
    if bid is None or ask is None:
        return None
    return _num(float((_dec(bid) + _dec(ask)) / 2))


def _parity_stock_bid(stock_quote: Any, source: str) -> Tuple[Optional[float], str]:
    """The IEX bid that may price the parity term, and its source label. A
    failed, one-sided or insane quote (the evaluation's own spread bound) is a
    failed read: the term is omitted, ``stock_quote_source="none"``."""
    q = stock_quote if isinstance(stock_quote, dict) else {}
    bid, ask = _pos(q.get('bid')), _pos(q.get('ask'))
    if bid is None or ask is None or ask / bid > _MAX_STOCK_SPREAD_RATIO:
        return None, 'none'
    return bid, source


def _chain_row_quote(opportunity: Any) -> Optional[Dict[str, Any]]:
    """The screened candidate's chain row as a quote dict (its stamp is the
    chain row's ``quote_timestamp``), or None."""
    row = opportunity.get('candidate') if isinstance(opportunity, dict) else None
    if not isinstance(row, dict):
        return None
    return {'bid': row.get('bid'), 'ask': row.get('ask'),
            'bid_size': row.get('bid_size'), 'ask_size': row.get('ask_size'),
            'timestamp': row.get('quote_timestamp')}


def _btc_pricing_fields(pricing: Any) -> Dict[str, Any]:
    """The log-only fields on every BTC row (R6-R): which reads priced it.

    A BTC row carries TWO IEX stock bids (review finding F7): ``stock_bid``
    (``_stock_fields``) is the EVALUATION-time bid — the one
    ``intrinsic_at_placement`` is computed from — while ``parity_stock_bid``
    here is the EXECUTE-time (attempt 0) or RE-PRICE (attempt n >= 1) bid that
    priced this limit's parity term, from the read ``stock_quote_source``
    names (null with ``"none"``: the term was omitted).
    ``improvement_floor_applied`` (F1) is true when a re-price's strict step
    (prior limit + one tick), not the formula, set the limit; null on attempt
    0, which replaces nothing."""
    p = pricing if isinstance(pricing, dict) else {}
    applied = p.get('parity_floor_applied')
    improved = p.get('improvement_floor_applied')
    return {
        'btc_quote_source': _str_or_none(p.get('quote_source')),
        'stock_quote_source': _str_or_none(p.get('stock_quote_source')),
        'parity_floor_applied': applied if isinstance(applied, bool) else None,
        'parity_stock_bid': _pos(p.get('stock_bid')),
        'limit_formula': _str_or_none(p.get('formula')),
        'improvement_floor_applied': (improved if isinstance(improved, bool)
                                      else None),
    }


def _rung1_basis(pre_quote: Any, pre_source: Any, fresh: Any,
                 formula: str) -> Dict[str, Any]:
    """Rung 1's basis, AFTER the BTC filled: ``min(pre_btc_bid, fresh_bid)``
    in base mode; in imminence, the lower of the two-sided mids. ``quote``
    (whose stamp is the row's ``quote_age_s``) is the fresh read when it was
    usable, else the pre-BTC quote — the post-BTC fallback F5 keeps, because
    the alternative is uncovered shares. ``source``: ``fresh`` / ``pre_btc`` /
    ``chain`` (the pre-BTC quote was the screened chain row — unreachable
    through ``execute_roll`` since F5, which never prices the pre-BTC screen
    off the chain; kept so the label stays honest if it ever is)."""
    pre = pre_quote if isinstance(pre_quote, dict) else {}
    fr = fresh if isinstance(fresh, dict) and fresh else {}
    pre_label = 'chain' if pre_source == 'chain' else 'pre_btc'
    out: Dict[str, Any] = {'pre_btc_bid': _pos(pre.get('bid')),
                           'pre_btc_source': _str_or_none(pre_source)}
    if formula == 'imminence':
        fresh_ok = _mid(fr) is not None
        two_sided = [q for q in (pre, fr) if _mid(q) is not None]
        chosen = min(two_sided, key=_mid) if two_sided else {}
        out.update(bid=chosen.get('bid'), ask=chosen.get('ask'),
                   basis_bid=_pos(chosen.get('bid')))
    else:
        fresh_ok = _pos(fr.get('bid')) is not None
        bids = [b for b in (_pos(pre.get('bid')), _pos(fr.get('bid')))
                if b is not None]
        out.update(bid=(min(bids) if bids else None), ask=None,
                   basis_bid=(min(bids) if bids else None))
    out.update(quote=(fr if fresh_ok else (pre or None)),
               source=('fresh' if fresh_ok else pre_label))
    return out


def _stc_ladder_fields(rung_quote: Any, ladder: Dict[str, Any],
                       prior: Any) -> Dict[str, Any]:
    """The log-only ladder fields on every STO row (DD-3). Total."""
    try:
        rq = rung_quote if isinstance(rung_quote, dict) else {}
        out = {
            'rung_kind': _str_or_none(rq.get('rung_kind')),
            'escalation_index': _int_or_none(rq.get('escalation_index')),
            'basis_bid': _pos(rq.get('basis_bid')),
            'stc_quote_source': _str_or_none(rq.get('stc_quote_source')),
            'primary_limit': _pos(ladder.get('primary_limit')),
            'limit_formula': _str_or_none(rq.get('limit_formula')),
        }
        if rq.get('rung') == 1:
            out['pre_btc_bid'] = _pos(rq.get('pre_btc_bid'))
            out['pre_btc_quote_source'] = _str_or_none(rq.get('pre_btc_quote_source'))
        if isinstance(prior, dict):
            out['prior_rung_limit'] = _pos(prior.get('limit'))
            out['prior_rung_disposition'] = _str_or_none(prior.get('disposition'))
        return out
    except Exception as exc:
        _instrumentation_failed('stc_ladder_fields', exc)
        return {}


def _record_prior(ladder: Dict[str, Any], rung: Any, limit: Any,
                  disposition: str, *, escalation_ok: bool,
                  ctx: Optional[Dict[str, Any]] = None) -> None:
    """Remember how the last placed (or synchronously refused) rung ended:
    the next rung's ``prior_rung_*`` fields, and whether an escalation rung
    may follow it (R6-B: only after the roller's own zero-fill settle, or a
    synchronous rejection)."""
    ladder['prior'] = {'rung': rung, 'limit': limit, 'disposition': disposition,
                       'escalation_ok': escalation_ok, 'ctx': ctx}
    if rung == 1:
        ladder['primary_disposition'] = disposition


def _miss_offsets(ctx: Dict[str, Any], filled_price: Any,
                  price_source: str) -> Dict[str, Any]:
    """On a fill of rung > 1: ``prior_rung_miss_offset`` (prior rung's limit -
    fill) and ``primary_miss_offset`` (rung 1's limit - fill; the DD-1 Rule B
    measurement). Each is null unless the rung it names MISSED (timed out or
    terminal zero fill — never a rejection) and the fill price is the
    broker's."""
    rung = ctx.get('rung')
    if not isinstance(rung, int) or isinstance(rung, bool) or rung <= 1:
        return {}
    broker = price_source == 'broker'
    ladder = ctx.get('ladder') if isinstance(ctx.get('ladder'), dict) else {}
    prior_missed = ctx.get('prior_rung_disposition') in _MISS_DISPOSITIONS
    primary_missed = ladder.get('primary_disposition') in _MISS_DISPOSITIONS
    return {
        'prior_rung_miss_offset': (_safe_diff(ctx.get('prior_rung_limit'),
                                              filled_price)
                                   if broker and prior_missed else None),
        'primary_miss_offset': (_safe_diff(ctx.get('primary_limit'), filled_price)
                                if broker and primary_missed else None),
    }


def _bounded_read(fn: Callable[..., Any], *args: Any,
                  timeout: Optional[float] = None, kind: str = 'diag'
                  ) -> Tuple[Optional[Dict[str, Any]], str]:
    """Run ONE bounded read under a hard wall-clock cap (ruling B).

    The read runs in a daemon worker thread; the caller waits at most
    ``timeout`` (default ``_DIAG_READ_TIMEOUT_SECONDS``, read at call time)
    and then walks away — a read that never returns is abandoned, never
    awaited, so it cannot delay or suppress anything. Returns
    ``(quote_dict, "ok")``, or ``(None, why)`` with ``why`` in ``timeout`` /
    ``error`` / ``empty``. Never raises.

    FC-120 PR-2: ``kind`` is ``"diag"`` (instrumentation) or ``"pricing"`` (a
    limit is computed from it; the caller has a fallback) — the worker's thread
    name says which. BOTH run inside ``alpaca_client.quiet_data_plane()``
    (R6-J): their failures record on the data-plane circuit breaker and never
    log an error-category row, so a quote outage can neither page as an error
    nor open the breaker that gates ``get_order_by_id``.

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
                with quiet_data_plane():
                    box['value'] = fn(*args)
            except BaseException as exc:   # noqa: B036 - a worker must not die loud
                box['error'] = exc

        worker = threading.Thread(
            target=work, daemon=True,
            name=('fc120-pricing-read' if kind == 'pricing' else 'fc120-diag-read'))
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
    opportunity. It keys every leg row of this roll together, and (FC-120
    PR-2, F1) salts every placement's ``client_order_id``."""
    try:
        opportunity['fc120'] = {'roll_id': uuid.uuid4().hex,
                                'btc_order_id': None}
    except Exception:
        pass


def _roll_ctx(opportunity: Dict[str, Any]) -> Dict[str, Any]:
    """The opportunity's FC-120 context (a throwaway dict if it is missing)."""
    ctx = opportunity.get('fc120') if isinstance(opportunity, dict) else None
    return ctx if isinstance(ctx, dict) else {}


def _client_order_salt(opportunity: Dict[str, Any], leg: str, index: Any) -> str:
    """One roller placement's idempotency salt (FC-120 PR-2, review finding
    F1): ``<roll_id>:<leg>:<n>`` — the roll, the leg, and the attempt (BTC,
    0-based) or the rung (STO, 1-based ladder position). Unique per placement
    within a roll, so a re-priced attempt or a later rung can never derive an
    earlier placement's ``client_order_id`` (which Alpaca refuses as a
    duplicate); the same for every call of one placement, so retrying that
    placement's HTTP request stays idempotent."""
    return f"{_roll_ctx(opportunity).get('roll_id')}:{leg}:{index}"


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
                             cycle_started_monotonic: float,
                             rolled: Optional[Dict[str, str]] = None) -> int:
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

    ``rolled`` (FC-120 PR-2, confirmation item 4) maps an old short-call
    symbol to the replacement this cycle rolled it into: the old symbol's row
    then carries ``rolled_this_cycle=True`` and ``replacement_symbol``, and
    one extra row samples the replacement with ``replacement_of``. An
    un-rolled call's row is unchanged.

    Returns the number of rows emitted (for tests); never raises.
    """
    emitted = 0
    rolled = rolled if isinstance(rolled, dict) else {}
    try:
        for option_symbol in option_symbols:
            elapsed = time.monotonic() - cycle_started_monotonic
            if elapsed >= QUOTE_SAMPLE_MAX_CYCLE_SECONDS:
                break
            replacement = _str_or_none(rolled.get(option_symbol))
            extra = ({'rolled_this_cycle': True,
                      'replacement_symbol': replacement} if replacement else None)
            if _sample_one(alpaca, option_symbol,
                           skip_reasons.get(option_symbol), elapsed, extra=extra):
                emitted += 1
            if replacement:
                elapsed = time.monotonic() - cycle_started_monotonic
                if elapsed >= QUOTE_SAMPLE_MAX_CYCLE_SECONDS:
                    break
                if _sample_one(alpaca, replacement, None, elapsed,
                               extra={'replacement_of': option_symbol}):
                    emitted += 1
    except Exception as exc:
        _instrumentation_failed('quote_sample', exc)
    return emitted


def _sample_one(alpaca: Any, option_symbol: Any, skip_reason: Any,
                elapsed: float, extra: Optional[Dict[str, Any]] = None) -> bool:
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
            **(extra or {}),
        )
        return True
    except Exception as exc:
        _instrumentation_failed('quote_sample', exc)
        return False
