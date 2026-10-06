"""Tests for CallRoller — FC-078 (daily, credit-only), superseding FC-006.

Every test names the regression it catches. The two-leg state machine is the
hard part of this FC, and its tests are the ones that keep paper money from
teaching us what the plan reviews already taught: an order left working while
the next one is placed, a partial fill reported as a whole one, a timeout
reported as "unfilled" when contracts were actually bought.

Test IDs map to docs/plans/fc-078.md §Tests (T-1 .. T-22). Mutation-check
instructions live on each test that carries one.
"""

import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, call, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.strategy import call_roller as call_roller_module
from src.strategy.call_roller import CallRoller
from src.risk.risk_manager import RiskManager


@pytest.fixture(autouse=True)
def _no_settle_sleep(monkeypatch):
    """Cancel-settle polling must not sleep in tests.

    The settle bound is a module constant rather than a config knob (it is not
    an operator decision), so tests drive it here. Default 0 = one read, which
    is the *old* single-re-read shape — every test that needs the polling
    behaviour raises it explicitly and stubs sleep.
    """
    monkeypatch.setattr(call_roller_module, '_CANCEL_SETTLE_TIMEOUT_SECONDS', 0)
    monkeypatch.setattr(call_roller_module.time, 'sleep', lambda _s: None)


class FakeClock:
    """A monotonic clock that moves only when told to (FC-120 PR-2).

    ``_poll_order_fill`` is a MONOTONIC deadline since FC-113 (b): with
    ``time.sleep`` stubbed and the real clock, a window that never sees a
    terminal status would spin for its full length in real time. ``sleep(s)``
    advances this clock by ``s``; ``advance`` models a read's round trip."""

    def __init__(self, start: float = 1000.0):
        self.now = start
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def fake_clock(monkeypatch):
    """Swap the roller module's ``time`` for a FakeClock-backed namespace."""
    import types
    clk = FakeClock()
    monkeypatch.setattr(call_roller_module, 'time', types.SimpleNamespace(
        monotonic=clk.monotonic, sleep=clk.sleep))
    return clk


# --------------------------------------------------------------------------- #
# Fixtures and builders
# --------------------------------------------------------------------------- #

OLD_EXPIRY = date(2026, 8, 7)
IN_HORIZON = OLD_EXPIRY + timedelta(days=14)      # 2026-08-21, the flagship
BEYOND_HORIZON = OLD_EXPIRY + timedelta(days=15)  # 2026-08-22


def occ(underlying: str, expiry: date, strike: float, kind: str = 'C') -> str:
    """Build an OCC symbol. Never hand-roll this format inline."""
    return (f"{underlying}{expiry.strftime('%y%m%d')}{kind}"
            f"{int(round(strike * 1000)):08d}")


OLD_SYMBOL = occ('GOOGL', OLD_EXPIRY, 370)


def candidate(strike: float, bid: float, ask: float, *, delta: float = 0.45,
              expiry: date = IN_HORIZON, underlying: str = 'GOOGL'):
    """A replacement-call dict shaped like find_suitable_calls output."""
    return {
        'symbol': occ(underlying, expiry, strike),
        'strike_price': strike,
        'delta': delta,
        'bid': bid,
        'ask': ask,
        'mid_price': round((bid + ask) / 2, 2),
        'expiration_date': expiry.isoformat(),
        'dte': (expiry - OLD_EXPIRY).days,
        'volume': 100,
        'open_interest': 500,
    }


# The flagship pair from the plan's DD-8 expectation table: C375 carries far
# more credit, C380 carries more contingent strike. Credit wins.
#
# FC-120 PR-2: every limit is now buffered 0.10 through its quote, so the BTC
# places at 8.50 (8.40 ask + 0.10) and a candidate is legal only if its
# BUFFERED bid clears that. C380's book moved from 8.50/8.70 (now a -$10
# debit at the placed limits) to 8.80/9.00 (+$20) so it stays the legal,
# smaller-credit fallback every ladder test below exercises. C375 nets +$230.
C375 = candidate(375.0, bid=10.90, ask=11.10, delta=0.48)
C380 = candidate(380.0, bid=8.80, ask=9.00, delta=0.35)


@pytest.fixture
def rolling_config():
    config = Mock()
    config.rolling_enabled = True
    config.rolling_itm_trigger_ratio = 0.98
    config.rolling_max_extension_days = 14
    config.rolling_max_replacement_delta = 0.60
    config.rolling_min_net_credit_per_contract = 0.00
    config.rolling_imminence_extrinsic_threshold = 0.20
    config.rolling_btc_fill_timeout_seconds = 0   # one poll, zero sleeps
    config.rolling_fallback_strike_attempts = 2
    # FC-120 PR-2 (item 10). Shipped values except the STO window, which is 0
    # for the same reason as the BTC's: one poll, zero sleeps per rung.
    config.rolling_btc_reprice_attempts = 2
    config.rolling_marketable_buffer_per_share = 0.10
    config.rolling_stc_rung_timeout_seconds = 0
    config.rolling_stc_escalation_rungs = 0
    config.roller_dry_run = False
    config.earnings_enabled = True
    return config


@pytest.fixture
def mock_alpaca():
    alpaca = Mock()
    alpaca.get_stock_quote.return_value = {'bid': 376.90, 'ask': 377.10}
    alpaca.get_option_quote.side_effect = _quote_book({
        OLD_SYMBOL: {'bid': 8.00, 'ask': 8.40, 'mid_price': 8.20},
        C375['symbol']: {'bid': 10.90, 'ask': 11.10, 'mid_price': 11.00},
        C380['symbol']: {'bid': 8.80, 'ask': 9.00, 'mid_price': 8.90},
    })
    alpaca.cancel_order.return_value = True
    return alpaca


def _quote_book(book):
    return lambda symbol: dict(book.get(symbol, {}))


@pytest.fixture
def mock_market_data():
    md = Mock()
    # Deliberately NOT credit-ordered: return_score order is the entry-shaped
    # ordering the roller must not defer to.
    md.find_suitable_calls.return_value = [C380, C375]
    return md


@pytest.fixture
def mock_risk_manager(rolling_config):
    return RiskManager(rolling_config)


@pytest.fixture
def mock_earnings():
    ec = Mock()
    ec.next_earnings_info.return_value = ('clear', None)
    ec.get_earnings_proximity.return_value = {
        'next_earnings_date': None, 'days_until': None, 'earnings_hour': None,
    }
    return ec


@pytest.fixture
def roller(mock_alpaca, mock_market_data, rolling_config, mock_risk_manager,
           mock_earnings):
    return CallRoller(mock_alpaca, mock_market_data, rolling_config,
                      mock_risk_manager, mock_earnings)


def call_position(symbol: str = OLD_SYMBOL, qty: str = '-1'):
    return {'symbol': symbol, 'qty': qty, 'asset_class': 'us_option'}


def stock_position(avg_entry_price: str = '300.00', qty: str = '100',
                   symbol: str = 'GOOGL'):
    return {'symbol': symbol, 'qty': qty, 'cost_basis': '30000.0',
            'avg_entry_price': avg_entry_price, 'asset_class': 'us_equity',
            'side': 'long'}


def events(mock_logger):
    """Every structured event emitted, in order, as (event_type, kwargs)."""
    out = []
    for sink in (mock_logger.info, mock_logger.error, mock_logger.warning):
        for c in sink.call_args_list:
            if c.kwargs.get('event_type'):
                out.append((c.kwargs['event_type'], c.kwargs))
    return out


def event_types(mock_logger):
    return [e for e, _ in events(mock_logger)]


TERMINAL_EVENTS = {
    'call_roll_skipped',
    'call_roll_skipped_cost_basis_unresolved',
    'call_roll_skipped_cost_basis_divergent',
    'call_roll_btc_rejected',
    'call_roll_btc_timeout_canceled',
    'call_roll_naked_exposure',
    # The STO covered fewer contracts than the BTC closed. Same class as
    # naked_exposure at a smaller quantity, and alert-wired alongside it.
    'call_roll_partial_naked_exposure',
    # A cancel that never settled: we do not know what the order did, so the
    # bot stopped touching the position. Alert-wired.
    'call_roll_unknown_disposition',
    'call_roll_completed',
    'call_roll_dry_run',
    'call_roll_execution_error',
}


def terminals(mock_logger):
    return [e for e in event_types(mock_logger) if e in TERMINAL_EVENTS]


def order(order_id: str, status: str, filled_qty: int = 0,
          filled_avg_price=None, qty: int = 1):
    return {'order_id': order_id, 'status': status, 'qty': qty,
            'filled_qty': filled_qty, 'filled_avg_price': filled_avg_price}


def accepted(order_id: str):
    return {'success': True, 'order_id': order_id}


# --------------------------------------------------------------------------- #
# T-3 — the quote-key regression that killed every evaluation (FC-066 cause 1)
# --------------------------------------------------------------------------- #

class TestQuoteKeys:
    """FC-066 cause 1. The roller read ``last_price``/``ask_price`` from a
    client that returns ``bid``/``ask``, so the stock price resolved to 0 and
    ``evaluate_roll_opportunity`` returned None before ANY gate — silently, five
    Fridays running, with ``rolls_evaluated > 0`` and ``rolls_executed = 0``.

    *Mutation:* reintroduce a ``last_price`` / ``ask_price`` read → these fail.
    """

    def test_bid_ask_keys_carry_the_evaluation_through(self, roller):
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())

        assert opp is not None
        # Stock price is the MID of the two-sided quote, not either side.
        assert opp['stock_price'] == pytest.approx(377.00)
        # BTC prices off `ask`, not `ask_price` — 8.40 + the FC-120 PR-2
        # buffer (0.10), on the nickel grid.
        assert opp['btc_limit'] == pytest.approx(8.50)

    def test_a_raising_stock_quote_emits_a_skip_not_a_crash(self, roller,
                                                            mock_alpaca):
        """get_stock_quote RAISES on failure — the as-built ``if not quote``
        branch was unreachable, so a data outage looked like "no roll today"."""
        mock_alpaca.get_stock_quote.side_effect = RuntimeError("alpaca down")

        with patch('src.strategy.call_roller.logger') as log:
            assert roller.evaluate_roll_opportunity(
                call_position(), stock_position()) is None

        skips = [k for e, k in events(log) if e == 'call_roll_skipped']
        assert len(skips) == 1
        assert skips[0]['skip_reason'] == 'stock_quote_unavailable'

    def test_a_zero_btc_ask_emits_a_skip_not_a_silent_none(self, roller,
                                                           mock_alpaca):
        mock_alpaca.get_option_quote.side_effect = _quote_book(
            {OLD_SYMBOL: {'bid': 0.0, 'ask': 0.0}})

        with patch('src.strategy.call_roller.logger') as log:
            assert roller.evaluate_roll_opportunity(
                call_position(), stock_position()) is None

        skips = [k for e, k in events(log) if e == 'call_roll_skipped']
        assert [s['skip_reason'] for s in skips] == ['btc_quote_unavailable']


# --------------------------------------------------------------------------- #
# T-22 — quote-quality guard
# --------------------------------------------------------------------------- #

class TestQuoteQualityGuard:
    """A one-sided or spread-insane quote must not gate a money decision.

    Live IEX example: AAPL bid 287.82 / ask 318.75 makes the ITM ratio 0.92 or
    1.02 depending on which side you trust.

    *Mutation:* restore the "whichever side is positive" fallback → these fail.
    """

    @pytest.mark.parametrize("quote,label", [
        ({'bid': 0.0, 'ask': 377.10}, "one-sided (no bid)"),
        ({'bid': 376.90, 'ask': 0.0}, "one-sided (no ask)"),
        ({'bid': 287.82, 'ask': 318.75}, "spread-insane (the AAPL shape)"),
    ])
    def test_unusable_stock_quotes_skip(self, roller, mock_alpaca, quote, label):
        mock_alpaca.get_stock_quote.return_value = quote

        with patch('src.strategy.call_roller.logger') as log:
            assert roller.evaluate_roll_opportunity(
                call_position(), stock_position()) is None, label

        skips = [k for e, k in events(log) if e == 'call_roll_skipped']
        assert [s['skip_reason'] for s in skips] == ['stock_quote_unusable'], label

    def test_a_spread_just_inside_the_bound_is_allowed(self, roller,
                                                       mock_alpaca):
        """The guard is a sanity bound, not a tick-tightness requirement: a
        4% spread on an ITM name still prices a decision."""
        mock_alpaca.get_stock_quote.return_value = {'bid': 370.0, 'ask': 384.0}

        assert roller.evaluate_roll_opportunity(
            call_position(), stock_position()) is not None

    def test_a_spread_just_outside_the_bound_is_refused(self, roller,
                                                        mock_alpaca):
        mock_alpaca.get_stock_quote.return_value = {'bid': 370.0, 'ask': 392.2}

        with patch('src.strategy.call_roller.logger') as log:
            assert roller.evaluate_roll_opportunity(
                call_position(), stock_position()) is None

        skips = [k for e, k in events(log) if e == 'call_roll_skipped']
        assert [s['skip_reason'] for s in skips] == ['stock_quote_unusable']

    def test_a_one_sided_option_quote_suppresses_imminence_mode(
            self, roller, mock_alpaca):
        """A mid needs two sides. Without them, base pricing applies and the
        override does NOT fire — no aggression on junk data."""
        mock_alpaca.get_option_quote.side_effect = _quote_book({
            OLD_SYMBOL: {'bid': 0.0, 'ask': 7.05},   # would be extrinsic 0.05
            C375['symbol']: C375,
            C380['symbol']: C380,
        })

        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())

        assert opp['pricing_mode'] == 'base'
        assert opp['extrinsic_per_share'] is None
        # The ask + buffer (FC-120 PR-2), not a mid: 7.05 + 0.10.
        assert opp['btc_limit'] == pytest.approx(7.15)


# --------------------------------------------------------------------------- #
# T-6 — eligibility
# --------------------------------------------------------------------------- #

class TestEligibility:

    def test_otm_is_the_profit_takers_territory(self, roller, mock_alpaca):
        mock_alpaca.get_stock_quote.return_value = {'bid': 359.90, 'ask': 360.10}

        with patch('src.strategy.call_roller.logger') as log:
            assert roller.evaluate_roll_opportunity(
                call_position(), stock_position()) is None

        skips = [k for e, k in events(log) if e == 'call_roll_skipped']
        assert skips[0]['skip_reason'] == 'not_itm_enough'

    def test_an_itm_call_at_dte_7_is_evaluated(self, roller, mock_alpaca):
        """FC-066 cause 2: the DTE <= 1 gate is what blinded the roller. A
        churned book's calls are 5-7 DTE on any given day.

        *Mutation:* reintroduce a ``max_current_dte`` gate → this fails.
        """
        far = OLD_EXPIRY + timedelta(days=7)
        pos = call_position(symbol=occ('GOOGL', far, 370))
        mock_alpaca.get_option_quote.side_effect = _quote_book({
            pos['symbol']: {'bid': 8.00, 'ask': 8.40},
            C375['symbol']: C375, C380['symbol']: C380,
        })
        # The horizon moves with the old expiry, so IN_HORIZON candidates stay
        # legal — that IS the expiry-relative frame.
        assert roller.evaluate_roll_opportunity(pos, stock_position()) is not None

    def test_an_invalid_strike_skips(self, roller):
        with patch('src.strategy.call_roller.logger') as log:
            assert roller.evaluate_roll_opportunity(
                call_position(symbol='NOT-AN-OCC-SYMBOL'), stock_position()) is None

        skips = [k for e, k in events(log) if e == 'call_roll_skipped']
        assert skips[0]['skip_reason'] in ('invalid_strike', 'stock_quote_unavailable')

    def test_imminence_fires_at_or_below_the_threshold(self, roller, mock_alpaca):
        """extrinsic = max(0, mid - max(0, spot - strike)). Spot 377, strike
        370 => intrinsic 7.00. A mid of 7.15 leaves 0.15 <= 0.20.

        *Mutation:* invert the comparison → this fails.
        """
        mock_alpaca.get_option_quote.side_effect = _quote_book({
            OLD_SYMBOL: {'bid': 7.05, 'ask': 7.25},   # mid 7.15
            C375['symbol']: C375, C380['symbol']: C380,
        })

        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())

        assert opp['pricing_mode'] == 'imminence'
        assert opp['extrinsic_per_share'] == pytest.approx(0.15)
        # BTC crosses UP half the spread; STO crosses DOWN. Both, or the
        # invariant is being tested against an unlike pair.
        assert opp['btc_limit'] == pytest.approx(7.20)     # 7.15 + 0.05
        assert opp['stc_limit'] == pytest.approx(10.95)    # C375 mid 11.00 - 0.05

    def test_imminence_does_not_fire_above_the_threshold(self, roller):
        """Base marks: mid 8.20 - intrinsic 7.00 = 1.20 of extrinsic."""
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())

        assert opp['pricing_mode'] == 'base'
        assert opp['extrinsic_per_share'] == pytest.approx(1.20)


# --------------------------------------------------------------------------- #
# T-17 — the open-order guard
# --------------------------------------------------------------------------- #

class TestOpenOrderConflict:
    """DD-4. /monitor places fire-and-forget DAY buy-to-close limits with no
    cancel, so at 15:30 the roller can see a short call with a live BTC working
    against it. Rolling it can fill BOTH buys — an unintended long call plus a
    sold replacement.

    *Mutation:* drop the open-order guard → this fails.
    """

    def test_a_live_open_order_on_the_symbol_skips(self, roller):
        with patch('src.strategy.call_roller.logger') as log:
            result = roller.evaluate_roll_opportunity(
                call_position(), stock_position(),
                open_order_symbols={OLD_SYMBOL})

        assert result is None
        skips = [k for e, k in events(log) if e == 'call_roll_skipped']
        assert [s['skip_reason'] for s in skips] == ['open_order_conflict']

    def test_the_same_book_without_the_open_order_proceeds(self, roller):
        assert roller.evaluate_roll_opportunity(
            call_position(), stock_position(),
            open_order_symbols={occ('AAPL', OLD_EXPIRY, 300)}) is not None

    def test_the_guard_does_not_cancel_the_profit_takers_order(self, roller,
                                                               mock_alpaca):
        """Skip, not cancel-first. The working order belongs to the
        profit-taker, which has precedence by design; canceling it inverts that
        precedence and races the fill anyway."""
        roller.evaluate_roll_opportunity(
            call_position(), stock_position(), open_order_symbols={OLD_SYMBOL})

        mock_alpaca.cancel_order.assert_not_called()
        mock_alpaca.place_option_order.assert_not_called()


# --------------------------------------------------------------------------- #
# T-1 / T-7b — the credit invariant and max-credit selection
# --------------------------------------------------------------------------- #

class TestCreditOnly:

    def test_a_debit_candidate_is_refused(self, roller, mock_market_data,
                                          mock_alpaca):
        """*Mutation:* flip the invariant to allow a $0.01 debit → this fails."""
        debit = candidate(375.0, bid=8.39, ask=8.60)   # 8.39 - 8.40 = -0.01
        mock_market_data.find_suitable_calls.return_value = [debit]
        mock_alpaca.get_option_quote.side_effect = _quote_book({
            OLD_SYMBOL: {'bid': 8.00, 'ask': 8.40},
            debit['symbol']: debit,
        })

        with patch('src.strategy.call_roller.logger') as log:
            assert roller.evaluate_roll_opportunity(
                call_position(), stock_position()) is None

        skips = [k for e, k in events(log) if e == 'call_roll_skipped']
        assert [s['skip_reason'] for s in skips] == ['no_credit_candidate']
        mock_alpaca.place_option_order.assert_not_called()

    def test_exactly_flat_is_legal_at_the_default_floor(self, roller,
                                                        mock_market_data,
                                                        mock_alpaca):
        """The invariant is ``>=``, not ``>``: a roll EXACTLY at the minimum on
        the PLACED limits executes. (FC-120 PR-2: the placed limits are the
        buffered ones — BTC 8.40 + 0.10 = 8.50, STO 8.60 - 0.10 = 8.50 — so
        this fixture's min of $0.00 is met with nothing to spare; the shipped
        minimum is $0.20/contract, one tick on these limits.)"""
        flat = candidate(375.0, bid=8.60, ask=8.80)
        mock_market_data.find_suitable_calls.return_value = [flat]
        mock_alpaca.get_option_quote.side_effect = _quote_book({
            OLD_SYMBOL: {'bid': 8.00, 'ask': 8.40}, flat['symbol']: flat})

        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())

        assert opp is not None
        assert opp['btc_limit'] == pytest.approx(8.50)
        assert opp['stc_limit'] == pytest.approx(8.50)
        assert opp['net_credit_per_contract'] == pytest.approx(0.0)

    def test_marks_showing_credit_do_not_rescue_limits_that_do_not(
            self, roller, mock_market_data, mock_alpaca):
        """The invariant is on the LIMIT PRICES ACTUALLY PLACED, not on marks.

        This candidate's *mid* (9.10) clears the BTC ask by 0.70, but base mode
        sells at the BID (8.20), which is a 0.20 debit. Refused.
        """
        marks_only = candidate(375.0, bid=8.20, ask=10.00)  # mid 9.10
        mock_market_data.find_suitable_calls.return_value = [marks_only]
        mock_alpaca.get_option_quote.side_effect = _quote_book({
            OLD_SYMBOL: {'bid': 8.00, 'ask': 8.40},
            marks_only['symbol']: marks_only})

        with patch('src.strategy.call_roller.logger') as log:
            assert roller.evaluate_roll_opportunity(
                call_position(), stock_position()) is None

        skips = [k for e, k in events(log) if e == 'call_roll_skipped']
        assert [s['skip_reason'] for s in skips] == ['no_credit_candidate']

    def test_a_positive_min_credit_knob_defers_a_marginal_roll(
            self, roller, rolling_config, mock_market_data, mock_alpaca):
        """The knob exists so a churn guard can be added without a code change."""
        rolling_config.rolling_min_net_credit_per_contract = 25.00  # $0.25/share
        thin = candidate(375.0, bid=8.60, ask=8.80)  # +$0.20/share = $20
        mock_market_data.find_suitable_calls.return_value = [thin]
        mock_alpaca.get_option_quote.side_effect = _quote_book({
            OLD_SYMBOL: {'bid': 8.00, 'ask': 8.40}, thin['symbol']: thin})

        assert roller.evaluate_roll_opportunity(
            call_position(), stock_position()) is None


class TestMaxCreditSelection:
    """T-7b (DD-3). Among legal candidates {+$20, +$230} the roller executes
    +$230 — even though the fixture returns them in ``return_score`` order with
    the small-credit one FIRST. (Credits on the FC-120 PR-2 buffered limits:
    BTC 8.50; C375 10.80; C380 8.70.)

    *Mutation:* revert to first-past-the-post on the returned order → this
    fails, exactly as tonight's book would have taken +$13 over +$248.
    """

    def test_the_largest_net_credit_wins(self, roller):
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())

        assert opp['new_option_symbol'] == C375['symbol']
        assert opp['new_strike'] == 375.0
        assert opp['net_credit_per_contract'] == pytest.approx(230.0)

    def test_the_smaller_credit_candidate_is_still_legal_just_not_chosen(
            self, roller):
        """Ranking, not filtering: C380 stays on the ladder as a fallback."""
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())

        symbols = [c['new_option_symbol'] for c in opp['fallback_candidates']]
        assert symbols == [C375['symbol'], C380['symbol']]

    def test_ties_break_toward_the_higher_strike(self, roller, mock_market_data,
                                                 mock_alpaca):
        low = candidate(375.0, bid=10.90, ask=11.10)
        high = candidate(380.0, bid=10.90, ask=11.10)
        mock_market_data.find_suitable_calls.return_value = [low, high]
        mock_alpaca.get_option_quote.side_effect = _quote_book({
            OLD_SYMBOL: {'bid': 8.00, 'ask': 8.40},
            low['symbol']: low, high['symbol']: high})

        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())

        assert opp['new_strike'] == 380.0


# --------------------------------------------------------------------------- #
# T-2 — the FC-013 span gate on the replacement, fail-closed
# --------------------------------------------------------------------------- #

class TestSpanGateOnTheReplacement:
    """The replacement is a NEW short call; it must not newly span a known
    report. The gate filters CANDIDATES — when it empties the set, the roller
    places no order at all rather than closing and staying uncovered.

    *Mutation:* drop ``exclude_expiry_on_or_after`` from the roller's
    ``find_suitable_calls`` call → these fail.
    """

    def test_a_known_date_is_passed_as_the_span_floor(self, roller,
                                                      mock_market_data,
                                                      mock_earnings):
        mock_earnings.next_earnings_info.return_value = ('known', date(2026, 8, 26))

        roller.evaluate_roll_opportunity(call_position(), stock_position())

        _, kwargs = mock_market_data.find_suitable_calls.call_args
        assert kwargs['exclude_expiry_on_or_after'] == date(2026, 8, 26)

    def test_clear_passes_no_span_floor(self, roller, mock_market_data):
        roller.evaluate_roll_opportunity(call_position(), stock_position())

        _, kwargs = mock_market_data.find_suitable_calls.call_args
        assert kwargs['exclude_expiry_on_or_after'] is None

    def test_unknown_skips_the_whole_roll(self, roller, mock_earnings,
                                          mock_market_data):
        """A span test with no date cannot clear any candidate."""
        mock_earnings.next_earnings_info.return_value = ('unknown', None)

        with patch('src.strategy.call_roller.logger') as log:
            assert roller.evaluate_roll_opportunity(
                call_position(), stock_position()) is None

        skips = [k for e, k in events(log) if e == 'call_roll_skipped']
        assert [s['skip_reason'] for s in skips] == ['earnings_unknown']
        mock_market_data.find_suitable_calls.assert_not_called()

    def test_a_missing_calendar_service_fails_closed(
            self, mock_alpaca, mock_market_data, rolling_config,
            mock_risk_manager):
        """Never fail open on a missing service."""
        naked = CallRoller(mock_alpaca, mock_market_data, rolling_config,
                           mock_risk_manager, earnings_calendar=None)

        with patch('src.strategy.call_roller.logger') as log:
            assert naked.evaluate_roll_opportunity(
                call_position(), stock_position()) is None

        skips = [k for e, k in events(log) if e == 'call_roll_skipped']
        assert [s['skip_reason'] for s in skips] == ['earnings_unknown']

    def test_earnings_disabled_applies_no_span_gate(
            self, mock_alpaca, mock_market_data, rolling_config,
            mock_risk_manager):
        """Same posture as the scanner: config off means gate off."""
        rolling_config.earnings_enabled = False
        naked = CallRoller(mock_alpaca, mock_market_data, rolling_config,
                           mock_risk_manager, earnings_calendar=None)

        assert naked.evaluate_roll_opportunity(
            call_position(), stock_position()) is not None

    def test_the_ladder_reuses_the_filtered_list_and_never_re_queries(
            self, roller, mock_market_data, mock_alpaca):
        """M-8. A fresh ``find_suitable_calls`` at execute time could admit
        candidates filtered under a different earnings-cache state or drifted
        deltas — and an unfiltered order is a money bug.

        *Mutation:* substitute a chain query for ``fallback_candidates`` in the
        STO ladder → this fails.
        """
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())
        mock_market_data.find_suitable_calls.reset_mock()

        mock_alpaca.place_option_order.side_effect = [
            accepted('btc-1'), accepted('sto-1'), accepted('sto-2'),
            accepted('sto-3'), accepted('sto-4'),
        ]
        mock_alpaca.get_order_by_id.side_effect = (
            lambda oid: order(oid, 'filled', 1, 8.40) if oid == 'btc-1'
            else order(oid, 'canceled', 0))

        roller.execute_roll(opp)

        mock_market_data.find_suitable_calls.assert_not_called()


# --------------------------------------------------------------------------- #
# T-4 — the cost-basis floor (FC-065 Phase 2), fail-closed
# --------------------------------------------------------------------------- #

class TestTheRollFloorComesFromTheSharedResolver:
    """Rolling places orders without passing ``execute_call_sale``, so its
    strike floor is the ONLY below-basis protection on that path.

    *Mutation:* drop ``cost_basis_per_share`` from the ``min_strike`` max() →
    these fail.
    """

    def test_an_unresolved_basis_blocks_the_roll(self, roller, mock_market_data):
        pos = stock_position()
        pos.pop('avg_entry_price')

        with patch('src.strategy.call_roller.logger') as log:
            assert roller.evaluate_roll_opportunity(call_position(), pos) is None

        # The chain was never even scanned — fail closed, not "scan and hope".
        mock_market_data.find_suitable_calls.assert_not_called()
        skips = [k for e, k in events(log)
                 if e == 'call_roll_skipped_cost_basis_unresolved']
        assert len(skips) == 1
        assert skips[0]['skip_reason'] == 'cost_basis_unresolved'
        assert skips[0]['cost_basis_source'] == 'unresolved'

    def test_a_zero_avg_entry_price_blocks_the_roll(self, roller,
                                                    mock_market_data):
        """FC-029 observed Alpaca reporting zero for assigned positions."""
        with patch('src.strategy.call_roller.logger') as log:
            assert roller.evaluate_roll_opportunity(
                call_position(), stock_position(avg_entry_price='0')) is None

        mock_market_data.find_suitable_calls.assert_not_called()
        assert 'call_roll_skipped_cost_basis_unresolved' in event_types(log)
        assert 'call_roll_skipped_cost_basis_divergent' not in event_types(log)

    def test_a_divergent_cross_check_blocks_the_roll_under_its_own_name(
            self, roller, mock_market_data):
        """"Resolved and vetoed" is not "unresolved": labelling it so hides the
        symbol from every ``*_divergent`` taxonomy."""
        with patch.object(roller.cost_basis_resolver, '_lookup_assignment_basis',
                          return_value={'expected_basis_per_share': 290.00,
                                        'reconstructed_shares': 100, 'lots': []}):
            with patch('src.strategy.call_roller.logger') as log:
                assert roller.evaluate_roll_opportunity(
                    call_position(), stock_position(avg_entry_price='320.00')) is None

        mock_market_data.find_suitable_calls.assert_not_called()
        skips = [k for e, k in events(log)
                 if e == 'call_roll_skipped_cost_basis_divergent']
        assert len(skips) == 1
        assert skips[0]['broker_basis'] == 320.00
        assert skips[0]['expected_basis'] == 290.00
        assert 'call_roll_skipped_cost_basis_unresolved' not in event_types(log)

    def test_the_resolved_basis_is_the_strike_floor(self, roller,
                                                    mock_market_data):
        """Alpaca says $373.00/share; ``cost_basis / qty`` says $300.00. The
        floor handed to the chain scan must be the resolver's number."""
        roller.evaluate_roll_opportunity(
            call_position(), stock_position(avg_entry_price='373.00'))

        args, kwargs = mock_market_data.find_suitable_calls.call_args
        assert args == ('GOOGL',)
        assert kwargs['min_strike_price'] == 373.00

    def test_the_roll_up_rule_still_floors_a_low_basis(self, roller,
                                                       mock_market_data):
        roller.evaluate_roll_opportunity(call_position(), stock_position())

        _, kwargs = mock_market_data.find_suitable_calls.call_args
        assert kwargs['min_strike_price'] == pytest.approx(370.01)

    def test_a_candidate_below_the_floor_is_rejected_by_validate_roll(
            self, roller, mock_market_data, mock_alpaca):
        below = candidate(374.0, bid=10.90, ask=11.10)
        mock_market_data.find_suitable_calls.return_value = [below]
        mock_alpaca.get_option_quote.side_effect = _quote_book({
            OLD_SYMBOL: {'bid': 8.00, 'ask': 8.40}, below['symbol']: below})

        assert roller.evaluate_roll_opportunity(
            call_position(), stock_position(avg_entry_price='375.00')) is None

    def test_a_strike_exactly_at_the_floor_is_accepted(self, roller,
                                                       mock_market_data,
                                                       mock_alpaca):
        """FC-065 doctrine: floor = Alpaca avg_entry_price, so an at-floor
        call-away books >= $0 equity. The gate rejects only ``strike < floor``."""
        at_floor = candidate(375.0, bid=10.90, ask=11.10)
        mock_market_data.find_suitable_calls.return_value = [at_floor]
        mock_alpaca.get_option_quote.side_effect = _quote_book({
            OLD_SYMBOL: {'bid': 8.00, 'ask': 8.40}, at_floor['symbol']: at_floor})

        assert roller.evaluate_roll_opportunity(
            call_position(), stock_position(avg_entry_price='375.00')) is not None


# --------------------------------------------------------------------------- #
# T-9 / T-21 — leg order and the pre-BTC invariant re-check
# --------------------------------------------------------------------------- #

class TestExecutionOrdering:

    def test_btc_is_placed_before_any_sto(self, roller, mock_alpaca):
        """STO-first would momentarily hold two short calls against 100 shares —
        a naked call Alpaca would either reject or margin. There is no
        acceptable STO-first sequence.

        *Mutation:* reorder the legs → this fails.
        """
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())
        mock_alpaca.place_option_order.side_effect = [
            accepted('btc-1'), accepted('sto-1')]
        mock_alpaca.get_order_by_id.side_effect = lambda oid: (
            order(oid, 'filled', 1, 8.40) if oid == 'btc-1'
            else order(oid, 'filled', 1, 10.90))

        roller.execute_roll(opp)

        placements = [c for c in mock_alpaca.place_option_order.call_args_list]
        assert placements[0].kwargs['side'] == 'buy'
        assert placements[0].kwargs['symbol'] == OLD_SYMBOL
        assert placements[1].kwargs['side'] == 'sell'
        assert placements[1].kwargs['symbol'] == C375['symbol']

    def test_the_credit_can_vanish_between_evaluation_and_execution(
            self, roller, mock_alpaca):
        """T-21. Evaluation quotes are minutes old by order time; losing the
        race HERE costs nothing, because no order exists yet.

        *Mutation:* drop the pre-BTC re-check → this fails.
        """
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())
        # The STO leg decays below the BTC limit before we place anything.
        mock_alpaca.get_option_quote.side_effect = _quote_book({
            C375['symbol']: {'bid': 8.00, 'ask': 8.20}})

        with patch('src.strategy.call_roller.logger') as log:
            result = roller.execute_roll(opp)

        assert result['success'] is False
        assert result['reason'] == 'credit_gone_at_execution'
        mock_alpaca.place_option_order.assert_not_called()
        skips = [k for e, k in events(log) if e == 'call_roll_skipped']
        assert [s['skip_reason'] for s in skips] == ['credit_gone_at_execution']

    def test_the_re_check_reprices_the_sto_leg_when_it_still_clears(
            self, roller, mock_alpaca):
        """It is a re-PRICE, not just a re-test: the placed limit is the fresh
        one, so the invariant holds against what actually goes on the book.
        (FC-120 PR-2: the fresh bid less the 0.10 buffer, 9.50 -> 9.40.)"""
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())
        mock_alpaca.get_option_quote.side_effect = _quote_book({
            C375['symbol']: {'bid': 9.50, 'ask': 9.70}})
        mock_alpaca.place_option_order.side_effect = [
            accepted('btc-1'), accepted('sto-1')]
        mock_alpaca.get_order_by_id.side_effect = lambda oid: (
            order(oid, 'filled', 1, 8.40) if oid == 'btc-1'
            else order(oid, 'filled', 1, 9.50))

        roller.execute_roll(opp)

        sto = mock_alpaca.place_option_order.call_args_list[1]
        assert sto.kwargs['limit_price'] == pytest.approx(9.40)


# --------------------------------------------------------------------------- #
# T-10 — BTC timeout: cancel, then verify the TRUE disposition
# --------------------------------------------------------------------------- #

class TestBtcTimeoutDisposition:
    """Pins the disposition, not merely that cancel was called.

    "Cancel rejected because already filled" is a FILL, not an error. The
    as-built code returned on timeout with the DAY order still live and reported
    ``btc_unfilled`` without ever looking again — a fiction that would have been
    logged while contracts were being bought.

    *Mutations:* drop the cancel → case (a) fails; drop the post-cancel re-fetch
    → case (b) fails.

    FC-120 PR-2: these pin ONE attempt's disposition, so re-pricing is off
    here (``btc_reprice_attempts = 0`` — one placement over the full window,
    the exhausted-attempts terminal). The re-price state machine is T-10.
    """

    def _armed(self, roller, mock_alpaca, refetch):
        roller.config.rolling_btc_reprice_attempts = 0
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())
        mock_alpaca.place_option_order.side_effect = [
            accepted('btc-1'), accepted('sto-1')]
        seen = {'polls': 0}

        def by_id(oid):
            if oid == 'btc-1':
                seen['polls'] += 1
                # First read is the in-flight poll (non-terminal -> timeout);
                # the read AFTER the cancel is the disposition.
                return (order('btc-1', 'new', 0) if seen['polls'] == 1
                        else refetch)
            return order(oid, 'filled', 1, 10.90)

        mock_alpaca.get_order_by_id.side_effect = by_id
        return opp

    def test_a_zero_fill_cancel_terminates_with_the_position_unchanged(
            self, roller, mock_alpaca):
        opp = self._armed(roller, mock_alpaca,
                          refetch=order('btc-1', 'canceled', 0))

        with patch('src.strategy.call_roller.logger') as log:
            result = roller.execute_roll(opp)

        mock_alpaca.cancel_order.assert_called_once_with('btc-1')
        assert result['reason'] == 'btc_timeout_canceled'
        assert terminals(log) == ['call_roll_btc_timeout_canceled']
        # No STO was placed against a call we still hold.
        assert mock_alpaca.place_option_order.call_count == 1

    def test_a_cancel_that_lost_the_race_proceeds_into_the_ladder(
            self, roller, mock_alpaca):
        """Case (b): the re-fetch shows FILLED. The terminal must be
        ``completed`` — never ``btc_timeout_canceled``."""
        opp = self._armed(roller, mock_alpaca,
                          refetch=order('btc-1', 'filled', 1, 8.40))

        with patch('src.strategy.call_roller.logger') as log:
            result = roller.execute_roll(opp)

        assert result['success'] is True
        assert terminals(log) == ['call_roll_completed']
        assert 'call_roll_btc_timeout_canceled' not in event_types(log)

    def test_a_partial_discovered_after_the_cancel_sizes_the_sto_to_the_fill(
            self, roller, mock_alpaca):
        """Case (c): remainder canceled, STO for the FILLED quantity."""
        opp = roller.evaluate_roll_opportunity(
            call_position(qty='-2'), stock_position(qty='200'))
        mock_alpaca.place_option_order.side_effect = [
            accepted('btc-1'), accepted('sto-1')]
        seen = {'polls': 0}

        def by_id(oid):
            if oid == 'btc-1':
                seen['polls'] += 1
                return (order('btc-1', 'partially_filled', 1, 8.40, qty=2)
                        if seen['polls'] == 1
                        else order('btc-1', 'canceled', 1, 8.40, qty=2))
            return order(oid, 'filled', 1, 10.90)

        mock_alpaca.get_order_by_id.side_effect = by_id

        with patch('src.strategy.call_roller.logger') as log:
            result = roller.execute_roll(opp)

        mock_alpaca.cancel_order.assert_called_once_with('btc-1')
        assert result['success'] is True
        assert result['contracts'] == 1
        assert terminals(log) == ['call_roll_completed']

    def test_a_partially_filled_poll_is_not_treated_as_terminal(
            self, roller, mock_alpaca):
        """As-built, ``_poll_order_fill`` returned on the FIRST
        ``partially_filled`` with the remainder still working — which is how the
        STO leg came to be sized off the requested quantity. It must keep
        polling and then cancel-and-verify."""
        opp = self._armed(roller, mock_alpaca,
                          refetch=order('btc-1', 'canceled', 0))
        roller.execute_roll(opp)

        # Cancel happened: the poll did NOT accept a non-terminal status as done.
        mock_alpaca.cancel_order.assert_called_once_with('btc-1')


# --------------------------------------------------------------------------- #
# T-10c / T-18b — the QUEUED-CANCEL broker model (review H-1)
# --------------------------------------------------------------------------- #

def queued_cancel_broker(order_id, *, pending_reads, then_status,
                         filled_qty=0, filled_price=None, qty=1):
    """A broker whose cancels are QUEUED, like Alpaca's really are.

    ``cancel_order`` returns success, but the order sits in ``pending_cancel``
    for ``pending_reads`` reads before resolving to ``then_status``. That window
    is the whole defect: an order in ``pending_cancel`` is still working and can
    still fill, so a single post-cancel read establishes nothing.
    """
    state = {'reads': 0}

    def by_id(oid):
        if oid != order_id:
            return order(oid, 'canceled', 0)
        state['reads'] += 1
        if state['reads'] <= pending_reads:
            return order(oid, 'pending_cancel', 0, qty=qty)
        return order(oid, then_status, filled_qty, filled_price, qty=qty)

    return by_id, state


class TestQueuedCancelsOnTheBtcLeg:
    """Alpaca cancels are queued. The shipped code did ONE re-read after the
    cancel and dispatched on ``filled_qty`` while ignoring a non-terminal
    status, so a BTC sitting in ``pending_cancel`` — still working, still able
    to fill — was reported as ``btc_timeout_canceled``.

    *Mutation:* replace the settle poll with a single re-read → these fail.
    """

    def _armed(self, roller, mock_alpaca, by_id):
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())
        mock_alpaca.place_option_order.side_effect = [
            accepted('btc-1'), accepted('sto-1')]
        polls = {'n': 0}
        inner = by_id

        def wrapped(oid):
            if oid != 'btc-1':
                # The STO leg is not under test here; let it fill cleanly so the
                # BTC disposition is the only variable.
                return order(oid, 'filled', 1, 10.90)
            polls['n'] += 1
            if polls['n'] == 1:
                return order('btc-1', 'new', 0)   # the fill poll times out
            return inner(oid)

        mock_alpaca.get_order_by_id.side_effect = wrapped
        return opp

    def test_a_pending_cancel_that_never_settles_is_reported_as_UNKNOWN(
            self, roller, mock_alpaca, monkeypatch):
        """The honest answer. Not "canceled, zero fill" — the order may still be
        working, and we must not sell a call against a short position whose size
        we cannot establish."""
        by_id, _ = queued_cancel_broker('btc-1', pending_reads=99,
                                        then_status='canceled')
        opp = self._armed(roller, mock_alpaca, by_id)

        with patch('src.strategy.call_roller.logger') as log:
            result = roller.execute_roll(opp)

        assert result['reason'] == 'btc_disposition_unknown'
        assert terminals(log) == ['call_roll_unknown_disposition']
        assert 'call_roll_btc_timeout_canceled' not in event_types(log)
        # And crucially: no sell was ever placed.
        sells = [c for c in mock_alpaca.place_option_order.call_args_list
                 if c.kwargs['side'] == 'sell']
        assert sells == []

    def test_a_fill_landing_after_the_cancel_is_seen_by_the_settle_poll(
            self, roller, mock_alpaca, monkeypatch):
        """The reviewer's probe shape: cancel pending at the verify read, fill
        lands a moment later. Polling to terminal sees it; one re-read does not."""
        monkeypatch.setattr(call_roller_module,
                            '_CANCEL_SETTLE_TIMEOUT_SECONDS', 15)
        by_id, state = queued_cancel_broker(
            'btc-1', pending_reads=2, then_status='filled',
            filled_qty=1, filled_price=8.40)
        opp = self._armed(roller, mock_alpaca, by_id)

        with patch('src.strategy.call_roller.logger') as log:
            result = roller.execute_roll(opp)

        assert state['reads'] > 1, "the settle poll re-read only once"
        assert result['success'] is True
        assert terminals(log) == ['call_roll_completed']
        assert 'call_roll_btc_timeout_canceled' not in event_types(log)

    def test_an_unreadable_order_is_not_a_verified_zero_fill(self, roller,
                                                             mock_alpaca):
        """An API blip used to mint a 'verified zero fill' that was never
        verified: _safe_get_order returned {} and the code read filled_qty 0."""
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())
        mock_alpaca.place_option_order.side_effect = [accepted('btc-1')]
        polls = {'n': 0}

        def by_id(oid):
            polls['n'] += 1
            if polls['n'] == 1:
                return order('btc-1', 'new', 0)
            raise RuntimeError("alpaca 503")

        mock_alpaca.get_order_by_id.side_effect = by_id

        with patch('src.strategy.call_roller.logger') as log:
            result = roller.execute_roll(opp)

        assert result['reason'] == 'btc_disposition_unknown'
        assert terminals(log) == ['call_roll_unknown_disposition']
        # The breadcrumb is emitted too, and it is alert-wired.
        assert 'call_roll_order_refetch_failed' in event_types(log)

    def test_the_production_settle_bound_actually_allows_polling(self):
        """The autouse fixture drives the settle bound to 0 so tests don't
        sleep — which means no other test can see the SHIPPED value. Without
        this assertion, setting the source constant to 0 (one read, no polling:
        precisely the H-1 defect) passes the entire suite.

        The bound must exceed the 5s poll interval, or "poll to terminal" is one
        read wearing a loop.
        """
        import inspect
        from src.strategy import roll_budget
        # FC-120 PR-2: the constant lives in the leaf `roll_budget` and the
        # roller re-exports it. Read the literal from source, not the module
        # attribute — the fixture has already replaced the latter.
        literal = re.search(r'^CANCEL_SETTLE_TIMEOUT_SECONDS = (\d+)',
                            inspect.getsource(roll_budget), re.M)
        assert literal, "the settle bound constant is gone"
        assert int(literal.group(1)) >= 5, (
            "the shipped cancel-settle bound is below the 5s poll interval, so "
            "it performs a single read — the H-1 defect, restored")
        assert re.search(
            r'^_CANCEL_SETTLE_TIMEOUT_SECONDS = '
            r'roll_budget\.CANCEL_SETTLE_TIMEOUT_SECONDS$',
            inspect.getsource(call_roller_module), re.M), (
            "the roller no longer re-exports roll_budget's settle bound")
        # And the settle reads at least twice whatever the window (R6-I).
        assert roll_budget.CANCEL_SETTLE_MIN_READS == 2

    def test_the_refetch_breadcrumb_is_emitted_once_not_per_read(
            self, roller, mock_alpaca, monkeypatch, fake_clock):
        monkeypatch.setattr(call_roller_module,
                            '_CANCEL_SETTLE_TIMEOUT_SECONDS', 20)
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())
        mock_alpaca.place_option_order.side_effect = [accepted('btc-1')]
        polls = {'n': 0}

        def by_id(oid):
            polls['n'] += 1
            if polls['n'] == 1:
                return order('btc-1', 'new', 0)
            raise RuntimeError("alpaca 503")

        mock_alpaca.get_order_by_id.side_effect = by_id

        with patch('src.strategy.call_roller.logger') as log:
            roller.execute_roll(opp)

        breadcrumbs = [e for e in event_types(log)
                       if e == 'call_roll_order_refetch_failed']
        assert len(breadcrumbs) == 1, (
            f"a persistent outage left {len(breadcrumbs)} breadcrumbs; "
            f"one per poll is the contract")


class TestQueuedCancelsOnTheStoLadder:
    """The reviewer's probe produced the worst outcome in the whole review here:
    an STO ladder that placed THREE sells which all filled, while the roller
    reported ``naked_exposure`` — "shares uncovered (no naked position)" —
    against two genuinely naked short calls.

    *Mutation:* replace the settle poll with a single re-read → these fail.
    """

    def _armed(self, roller, mock_alpaca, sto_by_id):
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())
        mock_alpaca.place_option_order.side_effect = [
            accepted('btc-1'), accepted('sto-1'), accepted('sto-2'),
            accepted('sto-3'), accepted('sto-4')]
        mock_alpaca.get_order_by_id.side_effect = lambda oid: (
            order('btc-1', 'filled', 1, 8.40) if oid == 'btc-1'
            else sto_by_id(oid))
        return opp

    def test_a_rung_stuck_in_pending_cancel_STOPS_the_ladder(self, roller,
                                                             mock_alpaca):
        """This is the two-live-sells window. If rung 1 will not settle, rung 2
        must not be placed — the shipped code placed it."""
        polls = {}

        def sto_by_id(oid):
            polls[oid] = polls.get(oid, 0) + 1
            if polls[oid] == 1:
                return order(oid, 'new', 0)        # fill poll times out
            return order(oid, 'pending_cancel', 0)  # cancel never settles

        opp = self._armed(roller, mock_alpaca, sto_by_id)

        with patch('src.strategy.call_roller.logger') as log:
            result = roller.execute_roll(opp)

        sells = [c for c in mock_alpaca.place_option_order.call_args_list
                 if c.kwargs['side'] == 'sell']
        assert len(sells) == 1, (
            f"{len(sells)} sells placed while rung 1 was still working — "
            f"that is {len(sells) - 1} potentially naked short call(s)")
        assert result['reason'] == 'stc_disposition_unknown'
        assert terminals(log) == ['call_roll_unknown_disposition']
        # And it must NOT claim there is no naked position.
        assert 'call_roll_naked_exposure' not in event_types(log)

    def test_a_rung_that_fills_after_its_cancel_is_that_rungs_success(
            self, roller, mock_alpaca, monkeypatch):
        monkeypatch.setattr(call_roller_module,
                            '_CANCEL_SETTLE_TIMEOUT_SECONDS', 15)
        polls = {}

        def sto_by_id(oid):
            polls[oid] = polls.get(oid, 0) + 1
            if polls[oid] == 1:
                return order(oid, 'new', 0)
            if polls[oid] == 2:
                return order(oid, 'pending_cancel', 0)
            return order(oid, 'filled', 1, 10.90)

        opp = self._armed(roller, mock_alpaca, sto_by_id)

        with patch('src.strategy.call_roller.logger') as log:
            result = roller.execute_roll(opp)

        assert result['success'] is True
        assert result['stc_order_id'] == 'sto-1'
        assert terminals(log) == ['call_roll_completed']
        sells = [c for c in mock_alpaca.place_option_order.call_args_list
                 if c.kwargs['side'] == 'sell']
        assert len(sells) == 1


# --------------------------------------------------------------------------- #
# T-19b — the STO partial remainder (review H-2 = trader M-1)
# --------------------------------------------------------------------------- #

class TestTheStoPartialRemainder:
    """BTC closes 2, STO covers 1 → 100 shares uncovered.

    The shipped code reported ``call_roll_completed`` with
    ``net_credit = 1xSTO - 2xBTC = -$590`` — a blended DEBIT labelled a credit —
    and emitted no naked-exposure-class event at all, contradicting the plan's
    own text. Latent on today's 1-lot book; a money-path contract violation
    regardless.

    *Mutation:* restore the mixed-quantity net_credit → these fail.
    """

    def _partial(self, roller, mock_alpaca):
        opp = roller.evaluate_roll_opportunity(
            call_position(qty='-2'), stock_position(qty='200'))
        mock_alpaca.place_option_order.side_effect = [
            accepted('btc-1'), accepted('sto-1')]
        mock_alpaca.get_order_by_id.side_effect = lambda oid: (
            order('btc-1', 'filled', 2, 8.40, qty=2) if oid == 'btc-1'
            else order('sto-1', 'canceled', 1, 10.90, qty=2))
        return opp

    def test_the_uncovered_remainder_gets_a_naked_exposure_class_terminal(
            self, roller, mock_alpaca):
        opp = self._partial(roller, mock_alpaca)

        with patch('src.strategy.call_roller.logger') as log:
            result = roller.execute_roll(opp)

        assert terminals(log) == ['call_roll_partial_naked_exposure']
        assert 'call_roll_completed' not in event_types(log)
        assert result['success'] is False
        assert result['contracts_replaced'] == 1
        assert result['contracts_uncovered'] == 1

    def test_no_blended_negative_is_ever_labelled_a_credit(self, roller,
                                                           mock_alpaca):
        """The credit invariant is a PER-CONTRACT guarantee, so only a
        per-contract number may be called a credit. 1x(10.90-8.40)x100 = +$250,
        not 1x10.90x100 - 2x8.40x100 = -$590."""
        opp = self._partial(roller, mock_alpaca)

        with patch('src.strategy.call_roller.logger') as log:
            result = roller.execute_roll(opp)

        event = [k for e, k in events(log)
                 if e == 'call_roll_partial_naked_exposure'][0]
        assert event['net_credit_on_replaced'] == pytest.approx(250.0)
        assert result['net_credit_on_replaced'] == pytest.approx(250.0)
        # Both legs reported explicitly, so the cash picture is not inferred
        # from a single blended figure.
        assert event['btc_cash_paid'] == pytest.approx(1680.0)   # 2 x 8.40 x 100
        assert event['stc_cash_received'] == pytest.approx(1090.0)  # 1 x 10.90 x 100
        assert event['contracts_uncovered'] == 1

    def test_a_fully_matched_roll_still_reports_completed(self, roller,
                                                          mock_alpaca):
        """The fix must not turn clean rolls into error terminals."""
        opp = roller.evaluate_roll_opportunity(
            call_position(qty='-2'), stock_position(qty='200'))
        mock_alpaca.place_option_order.side_effect = [
            accepted('btc-1'), accepted('sto-1')]
        mock_alpaca.get_order_by_id.side_effect = lambda oid: (
            order('btc-1', 'filled', 2, 8.40, qty=2) if oid == 'btc-1'
            else order('sto-1', 'filled', 2, 10.90, qty=2))

        with patch('src.strategy.call_roller.logger') as log:
            result = roller.execute_roll(opp)

        assert terminals(log) == ['call_roll_completed']
        assert result['success'] is True
        assert result['contracts'] == 2
        assert result['net_credit'] == pytest.approx(500.0)  # 2 x 2.50 x 100


# --------------------------------------------------------------------------- #
# T-19 — partial-fill truth
# --------------------------------------------------------------------------- #

class TestPartialFillTruth:
    """*Mutation:* pass the REQUESTED qty to the STO leg → this fails.

    Selling 2 calls when only 1 was closed writes a call against shares still
    pledged to the unclosed remainder.
    """

    def test_the_sto_is_sized_to_the_btc_fill_not_the_request(self, roller,
                                                              mock_alpaca):
        opp = roller.evaluate_roll_opportunity(
            call_position(qty='-2'), stock_position(qty='200'))
        mock_alpaca.place_option_order.side_effect = [
            accepted('btc-1'), accepted('sto-1')]
        mock_alpaca.get_order_by_id.side_effect = lambda oid: (
            order('btc-1', 'canceled', 1, 8.40, qty=2) if oid == 'btc-1'
            else order(oid, 'filled', 1, 10.90))

        with patch('src.strategy.call_roller.logger') as log:
            result = roller.execute_roll(opp)

        sto = mock_alpaca.place_option_order.call_args_list[1]
        assert sto.kwargs['qty'] == 1
        assert result['contracts'] == 1

        completed = [k for e, k in events(log) if e == 'call_roll_completed']
        assert completed[0]['contracts'] == 1
        partials = [k for e, k in events(log) if e == 'call_roll_partial_fill']
        assert partials[0]['leg'] == 'btc'
        assert partials[0]['unfilled_qty'] == 1


# --------------------------------------------------------------------------- #
# T-18 / T-5 — the STO ladder
# --------------------------------------------------------------------------- #

class TestStoLadder:
    """*Mutation:* drop the inter-rung cancel → the at-most-one-live assertion
    fails.

    As-built, a rung that timed out left its DAY limit working and the ladder
    placed the next sell on top of it: two live sells against one covered lot,
    and a late fill on rung 1 while rung 2 works is a genuine NAKED SHORT CALL.
    """

    def _arm_ladder(self, roller, mock_alpaca, order_states):
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())
        mock_alpaca.place_option_order.side_effect = [
            accepted('btc-1'), accepted('sto-1'), accepted('sto-2'),
            accepted('sto-3'), accepted('sto-4')]
        mock_alpaca.get_order_by_id.side_effect = lambda oid: (
            order('btc-1', 'filled', 1, 8.40) if oid == 'btc-1'
            else order_states(oid))
        return opp

    def test_every_working_rung_is_cancelled_and_verified_before_the_next(
            self, roller, mock_alpaca):
        """Every rung times out here, so every rung is still WORKING when the
        ladder wants to advance — the exact shape that produced two live sells
        against one covered lot as-built."""
        polls = {}

        def states(oid):
            polls[oid] = polls.get(oid, 0) + 1
            # Working during the poll, dead only after the cancel.
            return (order(oid, 'new', 0) if polls[oid] == 1
                    else order(oid, 'canceled', 0))

        opp = self._arm_ladder(roller, mock_alpaca, states)
        roller.execute_roll(opp)

        seq = [c for c in mock_alpaca.mock_calls
               if c[0] in ('place_option_order', 'cancel_order')]
        sells = [(i, c) for i, c in enumerate(seq)
                 if c[0] == 'place_option_order' and c.kwargs.get('side') == 'sell']
        assert len(sells) >= 3, "ladder did not advance past rung 2"

        placed_ids = ['sto-1', 'sto-2', 'sto-3', 'sto-4']
        for n, ((first, _), (second, _)) in enumerate(zip(sells, sells[1:])):
            between = seq[first + 1:second]
            cancels = [c.args[0] for c in between if c[0] == 'cancel_order']
            assert cancels == [placed_ids[n]], (
                f"rung {n + 1} ({placed_ids[n]}) was left working while rung "
                f"{n + 2} was placed — two live sells against one covered lot")

    def test_a_rung_that_died_on_its_own_needs_no_cancel(self, roller,
                                                         mock_alpaca):
        """The invariant is "at most one LIVE order", not "cancel everything".
        A rung that reached a terminal status is already dead; canceling it
        would be a pointless API call on a money path."""
        opp = self._arm_ladder(roller, mock_alpaca,
                               lambda oid: order(oid, 'canceled', 0))
        roller.execute_roll(opp)

        mock_alpaca.cancel_order.assert_not_called()

    def test_a_cancel_that_fails_because_it_filled_is_that_rungs_success(
            self, roller, mock_alpaca):
        """The whole point of cancel-then-verify. A cancel losing the race is a
        FILL, and reporting it as a failed rung would strand a covered lot the
        book has already sold."""
        polls = {'sto-1': 0}

        def states(oid):
            if oid == 'sto-1':
                polls['sto-1'] += 1
                return (order('sto-1', 'new', 0) if polls['sto-1'] == 1
                        else order('sto-1', 'filled', 1, 10.90))
            return order(oid, 'canceled', 0)

        opp = self._arm_ladder(roller, mock_alpaca, states)
        mock_alpaca.cancel_order.return_value = False  # cancel refused

        with patch('src.strategy.call_roller.logger') as log:
            result = roller.execute_roll(opp)

        assert result['success'] is True
        assert result['stc_order_id'] == 'sto-1'
        assert terminals(log) == ['call_roll_completed']
        # Rung 2 was never placed: rung 1 succeeded.
        sells = [c for c in mock_alpaca.place_option_order.call_args_list
                 if c.kwargs['side'] == 'sell']
        assert len(sells) == 1

    def test_the_ladder_reprices_to_the_invariant_floor_after_a_better_fill(
            self, roller, mock_alpaca):
        """Rung 2 = ``btc_filled_price + min_credit``, used only when the BTC
        filled BETTER than its limit. Never below the minimum — that is the
        invariant."""
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())
        mock_alpaca.place_option_order.side_effect = [
            accepted('btc-1'), accepted('sto-1'), accepted('sto-2'),
            accepted('sto-3'), accepted('sto-4')]
        mock_alpaca.get_order_by_id.side_effect = lambda oid: (
            order('btc-1', 'filled', 1, 8.00) if oid == 'btc-1'   # beat the limit
            else order(oid, 'canceled', 0))

        roller.execute_roll(opp)

        sells = [c.kwargs['limit_price']
                 for c in mock_alpaca.place_option_order.call_args_list
                 if c.kwargs['side'] == 'sell']
        # rung 1: the re-checked bid less the FC-120 PR-2 buffer (10.90 - 0.10)
        assert sells[0] == pytest.approx(10.80)
        assert sells[1] == pytest.approx(8.00)    # rung 2: the invariant floor
        assert all(p >= 8.00 for p in sells), "a rung priced below the invariant"

    def test_fallback_rungs_anchor_the_invariant_on_the_ACTUAL_BTC_FILL(
            self, roller, mock_alpaca):
        """Trader L-3. A mutation anchoring rungs 3+ on ``btc_limit`` instead of
        ``btc_filled_price`` survived the original suite, because every test
        filled the BTC exactly at its limit — where the two numbers coincide.
        They only diverge when the BTC fills BETTER than its limit, which is the
        common case and the one that unlocks candidates.

        Here BTC fills at 7.00 against an 8.40 limit, and C380 is quoted at
        8.00: legal against the actual fill (+$1.00 credit), rejected against
        the stale limit. Anchoring on the limit silently forfeits a real credit
        roll — conservative, but wrong, and invisible without this test.
        """
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())
        mock_alpaca.place_option_order.side_effect = [
            accepted('btc-1'), accepted('sto-1'), accepted('sto-2'),
            accepted('sto-3'), accepted('sto-4')]
        mock_alpaca.get_option_quote.side_effect = _quote_book({
            C375['symbol']: {'bid': 10.90, 'ask': 11.10},
            C380['symbol']: {'bid': 8.00, 'ask': 8.20},
        })
        filled = {'sto-3': True}

        def by_id(oid):
            if oid == 'btc-1':
                return order('btc-1', 'filled', 1, 7.00)   # beat the 8.40 limit
            if oid in filled:
                return order(oid, 'filled', 1, 8.00)
            return order(oid, 'canceled', 0)

        mock_alpaca.get_order_by_id.side_effect = by_id

        result = roller.execute_roll(opp)

        sold = [c.kwargs['symbol']
                for c in mock_alpaca.place_option_order.call_args_list
                if c.kwargs['side'] == 'sell']
        assert C380['symbol'] in sold, (
            "the C380 rung was never reached — the invariant is anchored on "
            "btc_limit (8.40) instead of the actual fill (7.00), forfeiting a "
            "legal +$1.00 credit roll")
        assert result['success'] is True
        assert result['net_credit'] == pytest.approx(100.0)  # (8.00-7.00)x1x100

    def test_fallback_rungs_re_validate_and_re_test_the_invariant(
            self, roller, mock_alpaca):
        """T-5. *Mutation:* remove ``validate_roll`` or the credit recomputation
        from the fallback path → this fails.

        C380's quote has collapsed below the actual BTC fill by the time the
        ladder reaches it, so it must be refused rather than sold at a debit.
        """
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())
        mock_alpaca.place_option_order.side_effect = [
            accepted('btc-1'), accepted('sto-1'), accepted('sto-2'),
            accepted('sto-3')]
        mock_alpaca.get_option_quote.side_effect = _quote_book({
            C375['symbol']: {'bid': 10.90, 'ask': 11.10},
            C380['symbol']: {'bid': 6.00, 'ask': 6.20},   # collapsed
        })
        mock_alpaca.get_order_by_id.side_effect = lambda oid: (
            order('btc-1', 'filled', 1, 8.40) if oid == 'btc-1'
            else order(oid, 'canceled', 0))

        with patch('src.strategy.call_roller.logger') as log:
            result = roller.execute_roll(opp)

        sold_symbols = [c.kwargs['symbol']
                        for c in mock_alpaca.place_option_order.call_args_list
                        if c.kwargs['side'] == 'sell']
        assert C380['symbol'] not in sold_symbols, (
            "sold a fallback strike at a debit against the actual BTC fill")
        assert result['reason'] == 'stc_failed_naked_exposure'
        assert terminals(log) == ['call_roll_naked_exposure']

    def test_the_ladder_re_validates_even_a_stored_candidate(self, roller,
                                                             mock_alpaca):
        """T-5, the other half. *Mutation:* remove ``validate_roll`` from the
        fallback path → this fails.

        Re-validating a candidate the evaluation already validated looks
        redundant, and that is exactly why it gets deleted. It is the last gate
        between the stored list and a live order: anything that puts a wrong
        entry on that list — a future caller building the opportunity by hand,
        a refactor that widens the list, a merge — reaches the order site
        otherwise. Here the list carries a strike BELOW the cost-basis floor.
        """
        opp = roller.evaluate_roll_opportunity(
            call_position(), stock_position(avg_entry_price='374.00'))
        below_floor = candidate(372.5, bid=12.00, ask=12.20)
        opp['fallback_candidates'].append({
            'candidate': below_floor,
            'new_option_symbol': below_floor['symbol'],
            'new_strike': 372.5,
            'stc_limit': 12.00,
            'net_credit_per_share': 3.60,
            'net_credit_per_contract': 360.0,
        })
        mock_alpaca.place_option_order.side_effect = [
            accepted('btc-1'), accepted('sto-1'), accepted('sto-2'),
            accepted('sto-3'), accepted('sto-4')]
        mock_alpaca.get_option_quote.side_effect = _quote_book({
            C375['symbol']: {'bid': 10.90, 'ask': 11.10},
            below_floor['symbol']: {'bid': 12.00, 'ask': 12.20},
        })
        mock_alpaca.get_order_by_id.side_effect = lambda oid: (
            order('btc-1', 'filled', 1, 8.40) if oid == 'btc-1'
            else order(oid, 'canceled', 0))

        roller.execute_roll(opp)

        sold = [c.kwargs['symbol']
                for c in mock_alpaca.place_option_order.call_args_list
                if c.kwargs['side'] == 'sell']
        assert below_floor['symbol'] not in sold, (
            "the ladder sold a strike below the cost-basis floor — the "
            "execute-time half of the floor (FC-062) is not binding")

    def test_an_exhausted_ladder_terminates_with_naked_exposure(self, roller,
                                                                mock_alpaca):
        opp = self._arm_ladder(roller, mock_alpaca,
                               lambda oid: order(oid, 'canceled', 0))

        with patch('src.strategy.call_roller.logger') as log:
            result = roller.execute_roll(opp)

        assert result['success'] is False
        assert result['reason'] == 'stc_failed_naked_exposure'
        assert terminals(log) == ['call_roll_naked_exposure']
        naked = [k for e, k in events(log) if e == 'call_roll_naked_exposure'][0]
        # The message must not claim a naked short position — the shares are
        # uncovered but long-only.
        assert 'no naked position' in naked['error_message']

    def test_no_wheel_state_calls_are_attempted_on_any_path(self, roller,
                                                            mock_alpaca):
        """T-9's second half + T-12. The fiction is deleted, not half-revived."""
        opp = self._arm_ladder(roller, mock_alpaca,
                               lambda oid: order(oid, 'canceled', 0))
        roller.execute_roll(opp)

        assert not hasattr(roller, 'wheel_state')

    @pytest.mark.parametrize("escalations", [0, 1, 3])
    def test_the_primary_ladder_has_two_plus_e_rungs(self, roller, mock_alpaca,
                                                     escalations):
        """T-17(e) (replaces FC-107's `test_the_ladders_fixed_rungs_are_exactly_two`
        — `LADDER_FIXED_LEGS` is gone; the per-position budget is
        `roll_budget.per_position_budget_seconds`, which counts these rungs).

        With no fallback candidates, a ladder whose every rung times out and
        settles zero-fill after the roller's own cancel places exactly
        `roll_budget.PRIMARY_LADDER_RUNGS(E)` sells on the primary: rung 1,
        E escalation rungs, the floor. Shipped E = 0 is the pre-PR-2 shape, 2.
        Counted through the ladder driver, because `_rungs` yields only rung 1,
        the floor and the fallbacks — the escalation rungs are placed between
        them by `_attempt_stc` (PR-2 item 3).
        """
        from src.strategy import roll_budget

        roller.config.rolling_stc_escalation_rungs = escalations
        roller.config.rolling_fallback_strike_attempts = 0
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())
        script = {'btc-1': {'poll': order('btc-1', 'filled', 1, 8.00)}}
        for i in range(1, 6):
            script[f'sto-{i}'] = {'poll': order(f'sto-{i}', 'new'),
                                  'after_cancel': order(f'sto-{i}', 'canceled')}
        broker = _Broker(script)
        mock_alpaca.place_option_order.side_effect = (
            [accepted('btc-1')] + [accepted(f'sto-{i}') for i in range(1, 6)])
        mock_alpaca.get_order_by_id.side_effect = broker.by_id
        mock_alpaca.cancel_order.side_effect = broker.cancel

        result = roller.execute_roll(opp)

        sells = [c.kwargs for c in mock_alpaca.place_option_order.call_args_list
                 if c.kwargs['side'] == 'sell']
        assert result['reason'] == 'stc_failed_naked_exposure'
        assert {s['symbol'] for s in sells} == {C375['symbol']}
        assert len(sells) == roll_budget.PRIMARY_LADDER_RUNGS(escalations)
        limits = [s['limit_price'] for s in sells]
        assert limits == sorted(limits, reverse=True), (
            "the primary ladder must only ever step DOWN toward the floor")
        assert limits[-1] == pytest.approx(8.00), "the last rung is the floor"



# --------------------------------------------------------------------------- #
# T-12 — state deletion
# --------------------------------------------------------------------------- #

class TestStateDependencyIsDeleted:
    """DD-6. ``STATE_STORAGE_BUCKET`` has been unset since project start, so
    nothing was ever persisted; the only consumer was the debit tolerance's
    ``original_premium``, and credit-only has no debit to tolerate.

    *Catches:* half-revival of the fiction.
    """

    def test_the_constructor_takes_no_wheel_state(self, mock_alpaca,
                                                  mock_market_data,
                                                  rolling_config,
                                                  mock_risk_manager):
        import inspect
        params = list(inspect.signature(CallRoller.__init__).parameters)
        assert 'wheel_state' not in params
        assert CallRoller(mock_alpaca, mock_market_data, rolling_config,
                          mock_risk_manager) is not None

    def test_the_module_references_no_wheel_state_methods(self):
        source = Path(__file__).resolve().parent.parent / 'src' / 'strategy' / 'call_roller.py'
        text = source.read_text()
        for banned in ('wheel_state', 'get_active_call_details',
                       'set_active_call_details', 'record_call_roll',
                       'get_roll_count', 'add_call_position',
                       'WheelStateManager'):
            assert banned not in text, f"{banned} survived the deletion"

    def test_the_debit_machinery_is_gone(self, roller):
        assert not hasattr(roller, '_check_debit_tolerance')
        source = Path(__file__).resolve().parent.parent / 'src' / 'strategy' / 'call_roller.py'
        text = source.read_text()
        for banned in ('debit_pct_of_premium', 'call_roll_blocked_debit',
                       'rolling_max_debit_pct', 'btc_limit_over_ask_pct',
                       'stc_limit_under_bid_pct', 'rolling_max_current_dte',
                       'rolling_max_rolls_per_position',
                       'rolling_earnings_blackout_days',
                       'is_earnings_within_n_days'):
            assert banned not in text, f"{banned} survived the deletion"


# --------------------------------------------------------------------------- #
# T-11 — the terminal-event taxonomy, exhaustively
# --------------------------------------------------------------------------- #

class TestTheTerminalTaxonomyIsExhaustive:
    """DD-5. Every short call evaluated emits EXACTLY ONE terminal event, and
    the taxonomy has no path outside it.

    This is the FC-066 lesson made executable. Five Fridays of production
    reported ``rolls_evaluated > 0, rolls_executed = 0`` and nothing else —
    three ``return None`` branches with no event between them, so the roller's
    total failure was indistinguishable from "nothing was eligible today".
    Walking every branch and counting is the only way to know that is fixed.
    """

    def _drive(self, roller, mock_alpaca, *, open_orders=None,
               stock_pos=None, dry_run=False):
        """Run one position all the way to a terminal, return the events."""
        with patch('src.strategy.call_roller.logger') as log:
            opp = roller.evaluate_roll_opportunity(
                call_position(), stock_pos or stock_position(),
                open_order_symbols=open_orders)
            if opp is not None:
                roller.execute_roll(opp)
            return terminals(log)

    # --- pre-order gates: every one of them ------------------------------- #

    def test_stock_quote_raises(self, roller, mock_alpaca):
        mock_alpaca.get_stock_quote.side_effect = RuntimeError("down")
        assert self._drive(roller, mock_alpaca) == ['call_roll_skipped']

    def test_stock_quote_empty(self, roller, mock_alpaca):
        mock_alpaca.get_stock_quote.return_value = {}
        assert self._drive(roller, mock_alpaca) == ['call_roll_skipped']

    def test_stock_quote_unusable(self, roller, mock_alpaca):
        mock_alpaca.get_stock_quote.return_value = {'bid': 0.0, 'ask': 377.0}
        assert self._drive(roller, mock_alpaca) == ['call_roll_skipped']

    def test_open_order_conflict(self, roller, mock_alpaca):
        assert self._drive(roller, mock_alpaca,
                           open_orders={OLD_SYMBOL}) == ['call_roll_skipped']

    def test_not_itm_enough(self, roller, mock_alpaca):
        mock_alpaca.get_stock_quote.return_value = {'bid': 359.9, 'ask': 360.1}
        assert self._drive(roller, mock_alpaca) == ['call_roll_skipped']

    def test_cost_basis_unresolved(self, roller, mock_alpaca):
        pos = stock_position()
        pos.pop('avg_entry_price')
        assert self._drive(roller, mock_alpaca, stock_pos=pos) == \
            ['call_roll_skipped_cost_basis_unresolved']

    def test_cost_basis_divergent(self, roller, mock_alpaca):
        with patch.object(roller.cost_basis_resolver, '_lookup_assignment_basis',
                          return_value={'expected_basis_per_share': 290.0,
                                        'reconstructed_shares': 100, 'lots': []}):
            got = self._drive(roller, mock_alpaca,
                              stock_pos=stock_position(avg_entry_price='320.00'))
        assert got == ['call_roll_skipped_cost_basis_divergent']

    def test_btc_quote_unavailable(self, roller, mock_alpaca):
        mock_alpaca.get_option_quote.side_effect = _quote_book({})
        assert self._drive(roller, mock_alpaca) == ['call_roll_skipped']

    def test_earnings_unknown(self, roller, mock_alpaca, mock_earnings):
        mock_earnings.next_earnings_info.return_value = ('unknown', None)
        assert self._drive(roller, mock_alpaca) == ['call_roll_skipped']

    def test_no_suitable_replacement(self, roller, mock_alpaca,
                                     mock_market_data):
        mock_market_data.find_suitable_calls.return_value = []
        assert self._drive(roller, mock_alpaca) == ['call_roll_skipped']

    def test_no_credit_candidate(self, roller, mock_alpaca, mock_market_data):
        debit = candidate(375.0, bid=1.00, ask=1.20)
        mock_market_data.find_suitable_calls.return_value = [debit]
        mock_alpaca.get_option_quote.side_effect = _quote_book({
            OLD_SYMBOL: {'bid': 8.00, 'ask': 8.40}, debit['symbol']: debit})
        assert self._drive(roller, mock_alpaca) == ['call_roll_skipped']

    def test_invalid_expiry(self, roller, mock_alpaca):
        """A parseable strike with an unparseable expiry: no horizon to bound
        the replacement by, so fail closed rather than roll unbounded."""
        with patch('src.strategy.call_roller.coerce_expiry_date',
                   side_effect=[None]):
            with patch('src.strategy.call_roller.logger') as log:
                assert roller.evaluate_roll_opportunity(
                    call_position(), stock_position()) is None
        assert terminals(log) == ['call_roll_skipped']

    # --- order-lifecycle terminals ---------------------------------------- #

    def test_credit_gone_at_execution(self, roller, mock_alpaca):
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())
        mock_alpaca.get_option_quote.side_effect = _quote_book(
            {C375['symbol']: {'bid': 1.00, 'ask': 1.20}})
        with patch('src.strategy.call_roller.logger') as log:
            roller.execute_roll(opp)
        assert terminals(log) == ['call_roll_skipped']

    def test_btc_rejected(self, roller, mock_alpaca):
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())
        mock_alpaca.place_option_order.return_value = {'success': False}
        with patch('src.strategy.call_roller.logger') as log:
            roller.execute_roll(opp)
        assert terminals(log) == ['call_roll_btc_rejected']

    def test_btc_timeout_canceled(self, roller, mock_alpaca):
        """FC-120 PR-2: every attempt times out and settles zero-fill after the
        roller's own cancel — two re-prices, three placements, and still
        exactly ONE terminal (the re-price rows are per-attempt)."""
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())
        mock_alpaca.place_option_order.side_effect = [
            accepted(f'btc-{i}') for i in (1, 2, 3)]
        polls = {}

        def by_id(oid):
            polls[oid] = polls.get(oid, 0) + 1
            return (order(oid, 'new', 0) if polls[oid] == 1
                    else order(oid, 'canceled', 0))

        mock_alpaca.get_order_by_id.side_effect = by_id
        with patch('src.strategy.call_roller.logger') as log:
            roller.execute_roll(opp)
        assert terminals(log) == ['call_roll_btc_timeout_canceled']
        assert event_types(log).count('call_roll_btc_repriced') == 2
        assert mock_alpaca.place_option_order.call_count == 3

    def test_quote_unusable_at_execution(self, roller, mock_alpaca):
        """FC-120 PR-2 (R6-E): both BTC quotes unusable before any order — the
        execute-time read empty AND the evaluation quote ask <= 0 — is a TRUE
        skip: no order was ever placed."""
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())
        opp['btc_quote'] = {'bid': 0.0, 'ask': 0.0}
        mock_alpaca.get_option_quote.side_effect = _quote_book(
            {C375['symbol']: {'bid': 10.90, 'ask': 11.10}})
        with patch('src.strategy.call_roller.logger') as log:
            result = roller.execute_roll(opp)
        assert terminals(log) == ['call_roll_skipped']
        assert result['reason'] == 'quote_unusable'
        mock_alpaca.place_option_order.assert_not_called()

    def test_a_refused_reprice_is_one_timeout_terminal_never_a_skip(
            self, roller, mock_alpaca):
        """R6-E: a BTC was placed and canceled, so a refused re-price is
        ``call_roll_btc_timeout_canceled`` with ``reprice_skipped_reason`` —
        ``call_roll_skipped`` means "no order was ever placed"."""
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())
        mock_alpaca.place_option_order.side_effect = [accepted('btc-1')]
        seen = {'n': 0, 'q': 0}

        def by_id(oid):
            seen['n'] += 1
            return order(oid, 'new' if seen['n'] == 1 else 'canceled', 0)

        mock_alpaca.get_order_by_id.side_effect = by_id
        base = mock_alpaca.get_option_quote.side_effect

        def quotes(symbol):
            if symbol == C375['symbol']:
                seen['q'] += 1
                if seen['q'] > 1:          # the credit is gone at re-price
                    return {'bid': 1.00, 'ask': 1.20}
            return base(symbol)

        mock_alpaca.get_option_quote.side_effect = quotes
        with patch('src.strategy.call_roller.logger') as log:
            result = roller.execute_roll(opp)
        assert terminals(log) == ['call_roll_btc_timeout_canceled']
        assert result['reason'] == 'btc_timeout_canceled'
        ev = [k for e, k in events(log)
              if e == 'call_roll_btc_timeout_canceled'][0]
        assert ev['reprice_skipped_reason'] == 'credit_gone'
        assert 'call_roll_skipped' not in event_types(log)

    def test_naked_exposure(self, roller, mock_alpaca):
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())
        mock_alpaca.place_option_order.side_effect = [
            accepted('btc-1'), accepted('sto-1'), accepted('sto-2'),
            accepted('sto-3'), accepted('sto-4')]
        mock_alpaca.get_order_by_id.side_effect = lambda oid: (
            order('btc-1', 'filled', 1, 8.40) if oid == 'btc-1'
            else order(oid, 'canceled', 0))
        with patch('src.strategy.call_roller.logger') as log:
            roller.execute_roll(opp)
        assert terminals(log) == ['call_roll_naked_exposure']

    def test_completed(self, roller, mock_alpaca):
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())
        mock_alpaca.place_option_order.side_effect = [
            accepted('btc-1'), accepted('sto-1')]
        mock_alpaca.get_order_by_id.side_effect = lambda oid: (
            order('btc-1', 'filled', 1, 8.40) if oid == 'btc-1'
            else order('sto-1', 'filled', 1, 10.90))
        with patch('src.strategy.call_roller.logger') as log:
            roller.execute_roll(opp)
        assert terminals(log) == ['call_roll_completed']

    def test_dry_run(self, roller, mock_alpaca, rolling_config):
        rolling_config.roller_dry_run = True
        assert self._drive(roller, mock_alpaca) == ['call_roll_dry_run']

    # --- and the taxonomy has no members outside the contract -------------- #

    def test_the_source_emits_no_terminal_outside_the_taxonomy(self):
        """Any new ``call_roll_*`` event added to the roller must be classified
        as terminal or non-terminal deliberately — not discovered in
        production."""
        # wheel_engine.py too: it emits call_roll_* names of its own (the
        # execution-error terminal; FC-120's quote sampler is called from it).
        strategy = Path(__file__).resolve().parent.parent / 'src' / 'strategy'
        source = ''.join((strategy / name).read_text()
                         for name in ('call_roller.py', 'wheel_engine.py'))
        emitted = set(re.findall(r'"(call_roll_[a-z_]+)"', source))
        non_terminal = {
            'call_roll_evaluated', 'call_roll_btc_placed', 'call_roll_btc_filled',
            'call_roll_stc_placed', 'call_roll_stc_filled',
            'call_roll_partial_fill', 'call_roll_stc_unfilled',
            'call_roll_stc_rejected', 'call_roll_order_refetch_failed',
            # Breadcrumb emitted inside the ladder when a rung will not settle;
            # the TERMINAL for that position is call_roll_unknown_disposition,
            # raised by execute_roll once the ladder returns.
            'call_roll_stc_disposition_unknown',
            # FC-120 PR-1: per-LEG rows, one per placed order — never a
            # position's terminal (a roll with three placements emits three).
            'call_roll_leg_settled',
            # Per-RUNG: an STO rung canceled at its timeout with zero fill.
            # The ladder moves on, so the position's terminal comes later.
            'call_roll_stc_timeout_canceled',
            # Breadcrumb: the post-settle instrumentation quote failed. The
            # disposition was already final; its fields log as None.
            'call_roll_quote_refresh_failed',
            # Breadcrumb: an FC-120 field/row builder swallowed a failure
            # (ruling C) — makes a lost or nulled row detectable.
            'call_roll_instrumentation_failed',
            # FC-120 ruling D: the read-only end-of-cycle quote sample, one
            # row per held short call AFTER every terminal of the cycle.
            'call_roll_quote_sample',
            # FC-120 PR-2: per-ATTEMPT — a BTC re-priced after the roller's own
            # zero-fill cancel. The leg's terminal comes later (fill → the
            # ladder; refusal/exhaustion → call_roll_btc_timeout_canceled).
            'call_roll_btc_repriced',
        }
        unclassified = emitted - TERMINAL_EVENTS - non_terminal
        assert not unclassified, (
            f"unclassified roll events: {sorted(unclassified)} — classify each "
            f"as terminal or non-terminal in TERMINAL_EVENTS")


# --------------------------------------------------------------------------- #
# T-16 — dry run
# --------------------------------------------------------------------------- #

class TestDryRun:

    def test_dry_run_places_neither_leg(self, roller, rolling_config,
                                        mock_alpaca):
        rolling_config.roller_dry_run = True
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())

        with patch('src.strategy.call_roller.logger') as log:
            result = roller.execute_roll(opp)

        mock_alpaca.place_option_order.assert_not_called()
        mock_alpaca.cancel_order.assert_not_called()
        assert result['success'] is False
        dry = [k for e, k in events(log) if e == 'call_roll_dry_run']
        assert len(dry) == 1
        # FC-120 PR-2: the EXECUTE-TIME-priced limits (DD-5) — buffered.
        assert dry[0]['would_be_btc_limit'] == pytest.approx(8.50)
        assert dry[0]['would_be_stc_limit'] == pytest.approx(10.80)
        assert dry[0]['would_be_stc_symbol'] == C375['symbol']
        assert dry[0]['btc_quote_source'] == 'execution'
        assert dry[0]['net_credit'] == pytest.approx(230.0)


# --------------------------------------------------------------------------- #
# The happy path, end to end
# --------------------------------------------------------------------------- #

class TestTheFlagshipRoll:
    """The GOOGL-class trade this FC was expedited to capture."""

    def test_a_credit_roll_completes_and_reports_the_truth(self, roller,
                                                           mock_alpaca):
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())
        mock_alpaca.place_option_order.side_effect = [
            accepted('btc-1'), accepted('sto-1')]
        mock_alpaca.get_order_by_id.side_effect = lambda oid: (
            order('btc-1', 'filled', 1, 8.30) if oid == 'btc-1'
            else order('sto-1', 'filled', 1, 11.00))

        with patch('src.strategy.call_roller.logger') as log:
            result = roller.execute_roll(opp)

        assert result['success'] is True
        assert result['old_strike'] == 370.0
        assert result['new_strike'] == 375.0
        # Fill credit = STO fill - BTC fill, in dollars. 11.00 - 8.30 = 2.70.
        assert result['net_credit'] == pytest.approx(270.0)
        assert terminals(log) == ['call_roll_completed']

        completed = [k for e, k in events(log) if e == 'call_roll_completed'][0]
        assert completed['net_credit'] == pytest.approx(270.0)
        assert completed['pricing_mode'] == 'base'
        assert completed['contracts'] == 1
        assert 'roll_count' not in completed

    def test_a_btc_rejected_AFTER_placement_says_rejected_not_canceled(
            self, roller, mock_alpaca):
        """Trader L-2. A broker rejection reaching a terminal ``rejected``
        status was reported as ``call_roll_btc_timeout_canceled`` with
        ``disposition=terminal_no_fill``. Both mean "no fill", but they call for
        different investigations — a rejection is an account or contract
        problem that will recur every cycle until someone looks."""
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())
        mock_alpaca.place_option_order.side_effect = [accepted('btc-1')]
        mock_alpaca.get_order_by_id.side_effect = lambda oid: order(
            'btc-1', 'rejected', 0)

        with patch('src.strategy.call_roller.logger') as log:
            result = roller.execute_roll(opp)

        assert result['reason'] == 'btc_rejected'
        assert terminals(log) == ['call_roll_btc_rejected']
        rejected = [k for e, k in events(log) if e == 'call_roll_btc_rejected'][0]
        assert rejected['rejected_after_placement'] is True

    def test_a_rejected_btc_terminates_without_touching_the_position(
            self, roller, mock_alpaca):
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())
        mock_alpaca.place_option_order.return_value = {
            'success': False, 'error_message': 'insufficient buying power'}

        with patch('src.strategy.call_roller.logger') as log:
            result = roller.execute_roll(opp)

        assert result['reason'] == 'btc_rejected'
        assert terminals(log) == ['call_roll_btc_rejected']
        assert mock_alpaca.place_option_order.call_count == 1



# =========================================================================== #
# FC-120 PR-1 — the quote every roll leg is priced from (docs/plans/fc-120.md
# §Tests T-1 .. T-5, plus the rev-4 code-review rulings A-E). LOGGING ONLY:
# TestNoBehaviourChange pins that the new reads never reach a limit, and
# TestTheOrderPathIsMains pins that the broker-call sequence between order
# actions is main's and that no diagnostic read sits inside it.
#
# Module helpers are reached through call_roller_module (never imported by
# name) so this file still IMPORTS against main's call_roller.py — which is how
# the main-sequence goldens below are re-derived (FC120_PRINT_GOLDEN=1).
# =========================================================================== #

import os  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from datetime import timezone  # noqa: E402

cr = call_roller_module

PRIMITIVES = (str, int, float, bool, type(None))
INSTRUMENTED_EVENTS = (
    'call_roll_btc_placed', 'call_roll_stc_placed', 'call_roll_btc_filled',
    'call_roll_stc_filled', 'call_roll_btc_timeout_canceled',
    'call_roll_stc_timeout_canceled', 'call_roll_stc_unfilled',
    'call_roll_leg_settled', 'call_roll_dry_run', 'call_roll_evaluated',
    'call_roll_quote_sample', 'call_roll_quote_refresh_failed',
    'call_roll_instrumentation_failed')


def _stamp(age_s: float) -> datetime:
    """An aware-UTC broker stamp ``age_s`` seconds old — the live shape."""
    return datetime.now(timezone.utc) - timedelta(seconds=age_s)


def _q(bid, ask, *, age_s=40.0, stamp='auto', bid_size=10, ask_size=12):
    """An AlpacaClient.get_option_quote payload, broker stamp included."""
    return {'bid': bid, 'ask': ask, 'mid_price': round((bid + ask) / 2, 2),
            'bid_size': bid_size, 'ask_size': ask_size, 'feed': 'indicative',
            'timestamp': _stamp(age_s) if stamp == 'auto' else stamp}


def _is_diag_thread():
    """True on a bounded INSTRUMENTATION read's worker (FC-120 PR-1 ruling B).
    PR-2's bounded PRICING reads run on their own worker, named apart."""
    return threading.current_thread().name == 'fc120-diag-read'


def _sequenced(seqs):
    """get_option_quote stub with a per-SYMBOL counter.

    Entry 0 is the PRICED quote: every pricing read of the symbol returns it
    (the evaluation's main-thread read and, since FC-120 PR-2, the bounded
    execute-time / rung-basis / re-quote reads). Entries 1, 2, ... are the
    DIAGNOSTIC reads in order — 1 the re-read taken right after that leg is
    placed, 2 the post-settle read — the last entry repeating. An Exception
    entry raises. ``seqs[symbol]`` may instead be ``{'pricing': [...],
    'diag': [...]}`` to script the two independently (each list in call
    order, last entry repeating)."""
    counts = {}

    def pick(seq, n):
        item = seq[min(n, len(seq) - 1)]
        if isinstance(item, Exception):
            raise item
        return dict(item)

    def quote(symbol):
        seq = seqs.get(symbol, [{}])
        diag = _is_diag_thread()
        key = (symbol, 'diag' if diag else 'pricing')
        n = counts.get(key, 0)
        counts[key] = n + 1
        counts[symbol] = counts.get(symbol, 0) + 1
        if isinstance(seq, dict):
            return pick(seq['diag' if diag else 'pricing'], n)
        return pick(seq, n + 1 if diag else 0)
    quote.counts = counts
    return quote


def _book(old=(8.00, 8.40), c375=(10.90, 11.10), c380=(8.80, 9.00),
          shift=0.02, **overrides):
    """The instrumented book: each symbol's re-read is shifted by ``shift``
    on both sides; every later read returns the base quote again."""
    seqs = {}
    for sym, (bid, ask) in ((OLD_SYMBOL, old), (C375['symbol'], c375),
                            (C380['symbol'], c380)):
        base = _q(bid, ask)
        seqs[sym] = [base, _q(round(bid + shift, 2), round(ask + shift, 2)),
                     base]
    seqs.update(overrides)
    return _sequenced(seqs)


def _o(oid, status, filled=0, price=None, *, latency_s=None, qty=1):
    """An order dict carrying Alpaca's submitted_at / filled_at strings."""
    out = order(oid, status, filled, price, qty=qty)
    out['submitted_at'] = '2026-09-28T19:30:00+00:00'
    out['filled_at'] = (None if latency_s is None else
                        (datetime(2026, 9, 28, 19, 30, tzinfo=timezone.utc)
                         + timedelta(seconds=latency_s)).isoformat())
    return out


class _Broker:
    """Order state per id: ``poll`` until canceled, ``after_cancel`` after
    (a list is read in sequence, its last entry repeating)."""

    def __init__(self, script):
        self.script = script
        self.canceled = set()
        self.reads = {}

    def by_id(self, oid):
        entry = self.script[oid]
        canceled = oid in self.canceled and 'after_cancel' in entry
        seq = entry['after_cancel'] if canceled else entry['poll']
        seq = seq if isinstance(seq, list) else [seq]
        n = self.reads.get((oid, canceled), 0)
        self.reads[(oid, canceled)] = n + 1
        return dict(seq[min(n, len(seq) - 1)])

    def cancel(self, oid):
        self.canceled.add(oid)
        return True


@pytest.fixture
def instrumented(mock_alpaca, mock_market_data):
    """Stamped quotes everywhere: option book, IEX stock quote, chain rows."""
    mock_alpaca.get_stock_quote.return_value = {
        'bid': 376.90, 'ask': 377.10, 'timestamp': _stamp(1.0)}
    mock_alpaca.get_option_quote.side_effect = _book()
    mock_market_data.find_suitable_calls.return_value = [
        dict(C380, quote_timestamp=_stamp(100.0)),
        dict(C375, quote_timestamp=_stamp(100.0))]
    return mock_alpaca


def _order_ids(script, n_orders):
    """Placement ids in placement order: the script's own keys first (it
    lists its orders in the order they are placed — `btc-1`, `btc-2`, ... for
    re-priced attempts, then `sto-1`, ...), padded with unscripted ids."""
    ids = list(script)
    ids += [f'unscripted-{i}' for i in range(len(ids), max(n_orders, len(ids)) + 4)]
    return ids


def _drive(roller, mock_alpaca, script, n_orders=4):
    """Evaluate + execute one roll against a scripted broker."""
    opp = roller.evaluate_roll_opportunity(call_position(), stock_position())
    assert opp is not None
    broker = _Broker(script)
    mock_alpaca.place_option_order.side_effect = [
        accepted(oid) for oid in _order_ids(script, n_orders)]
    mock_alpaca.get_order_by_id.side_effect = broker.by_id
    mock_alpaca.cancel_order.side_effect = broker.cancel
    with patch('src.strategy.call_roller.logger') as log:
        result = roller.execute_roll(opp)
    return result, log


def _first(log, event_type):
    found = _all(log, event_type)
    assert found, f"{event_type} was never emitted: {event_types(log)}"
    return found[0]


def _all(log, event_type):
    return [kw for e, kw in events(log) if e == event_type]


#: BTC fills at 8.35 (5c through its 8.40 limit), rung 1 fills at 10.95.
FULL_FILL = {
    'btc-1': {'poll': _o('btc-1', 'filled', 1, 8.35, latency_s=5)},
    'sto-1': {'poll': _o('sto-1', 'filled', 1, 10.95, latency_s=4)},
}

#: BTC fills at 8.35; rungs 1 and 2 time out (the cancel settles zero-fill);
#: rung 3 (the C380 fallback) fills.
LADDER_TO_RUNG_3 = {
    'btc-1': {'poll': _o('btc-1', 'filled', 1, 8.35, latency_s=5)},
    'sto-1': {'poll': _o('sto-1', 'new'),
              'after_cancel': _o('sto-1', 'canceled')},
    'sto-2': {'poll': _o('sto-2', 'new'),
              'after_cancel': _o('sto-2', 'canceled')},
    'sto-3': {'poll': _o('sto-3', 'filled', 1, 8.55, latency_s=3)},
}

#: The BTC never fills: every attempt's poll sees `new` and its cancel settles
#: zero-fill. FC-120 PR-2: the shipped `btc_reprice_attempts = 2` re-prices
#: twice, so three attempts are scripted; a test that pins ONE attempt's
#: timeout path sets the attempts to 0 and only `btc-1` is ever placed.
BTC_TIMEOUT = {
    f'btc-{i}': {'poll': _o(f'btc-{i}', 'new'),
                 'after_cancel': _o(f'btc-{i}', 'canceled')}
    for i in (1, 2, 3)
}


def _expired(oid):
    return {'poll': _o(oid, 'expired')}


def _times_out(oid):
    return {'poll': _o(oid, 'new'), 'after_cancel': _o(oid, 'canceled')}


BTC_FILLED = {'poll': _o('btc-1', 'filled', 1, 8.35, latency_s=5)}

#: BTC fills; every rung ends terminal zero-fill on its first poll.
LADDER_EXHAUSTED = {'btc-1': BTC_FILLED, 'sto-1': _expired('sto-1'),
                    'sto-2': _expired('sto-2'), 'sto-3': _expired('sto-3')}

#: BTC fills; every rung times out and its cancel settles zero-fill.
ALL_RUNGS_TIME_OUT = {'btc-1': BTC_FILLED, 'sto-1': _times_out('sto-1'),
                      'sto-2': _times_out('sto-2'), 'sto-3': _times_out('sto-3')}


class _Timeline:
    """One ordered record of every broker call AND every log event of a roll.

    Broker calls are tagged by KIND — ``place`` / ``cancel`` / ``get_order`` /
    ``quote`` / ``stock_quote`` — when made on the test's own thread (which is
    where main makes every call, and where the roller makes every PRICING
    read), and ``diag`` when made on any other thread: FC-120's bounded
    instrumentation reads run in a daemon worker (ruling B), so the thread is
    the discriminator. ``on_diag`` lets a test block or poison diag reads only.
    """

    KINDS = {'place_option_order': 'place', 'cancel_order': 'cancel',
             'get_order_by_id': 'get_order', 'get_option_quote': 'quote',
             'get_stock_quote': 'stock_quote'}

    def __init__(self, on_diag=None):
        self.entries = []
        self.main = threading.get_ident()
        self.on_diag = on_diag

    def wrap(self, mock_alpaca):
        for name, kind in self.KINDS.items():
            inner = getattr(mock_alpaca, name)
            setattr(mock_alpaca, name,
                    Mock(side_effect=self._recorder(name, kind, inner)))

    def _recorder(self, name, kind, inner):
        def call_(*args, **kwargs):
            # FC-120 PR-2: the bounded PRICING reads also run off the main
            # thread, on a worker named apart from the diagnostic one — they
            # are order-path reads (``pricing_quote`` / ``pricing_stock_quote``)
            # and are never poisoned; only ``diag`` reads are.
            if threading.get_ident() == self.main:
                tag = kind
            elif threading.current_thread().name == 'fc120-pricing-read':
                tag = f'pricing_{kind}'
            else:
                tag = 'diag'
            if kind == 'place':
                key = (kwargs.get('symbol'), kwargs.get('side'),
                       kwargs.get('limit_price'))
            else:
                key = args[0] if args else None
            self.entries.append((tag, key))
            if tag == 'diag' and self.on_diag is not None:
                return self.on_diag(name, args, lambda: inner(*args, **kwargs))
            return inner(*args, **kwargs)
        return call_

    def log_sink(self, mock_logger):
        def sink(*_args, **kwargs):
            if kwargs.get('event_type'):
                self.entries.append(('log', kwargs['event_type']))
        for level in (mock_logger.info, mock_logger.error,
                      mock_logger.warning):
            level.side_effect = sink

    def order_path(self):
        """The broker calls main also makes: everything but ``diag``/``log``."""
        return [e for e in self.entries if e[0] not in ('diag', 'log')]


def _drive_timeline(roller, mock_alpaca, script, *, on_diag=None, n_orders=4):
    """``_drive`` with a ``_Timeline`` recording calls and events in order."""
    opp_timeline = _Timeline(on_diag=on_diag)
    opp_timeline.wrap(mock_alpaca)
    broker = _Broker(script)
    mock_alpaca.place_option_order.side_effect = opp_timeline._recorder(
        'place_option_order', 'place', Mock(side_effect=[
            accepted(oid) for oid in _order_ids(script, n_orders)]))
    mock_alpaca.get_order_by_id.side_effect = opp_timeline._recorder(
        'get_order_by_id', 'get_order', broker.by_id)
    mock_alpaca.cancel_order.side_effect = opp_timeline._recorder(
        'cancel_order', 'cancel', broker.cancel)
    with patch('src.strategy.call_roller.logger') as log:
        opp_timeline.log_sink(log)
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())
        assert opp is not None
        result = roller.execute_roll(opp)
    return result, log, opp_timeline


#: fixture name -> broker script (all on the instrumented base book)
#: fixture name -> (broker script, btc_reprice_attempts). ``btc_timeout_single``
#: is the one-attempt timeout (re-pricing off) — the shape main ran, so its
#: STRUCTURE can be compared with main's golden one-for-one.
ORDER_PATH_FIXTURES = {
    'full_fill': (FULL_FILL, 2),
    'btc_timeout': (BTC_TIMEOUT, 2),
    'btc_timeout_single': (BTC_TIMEOUT, 0),
    'ladder_to_rung_3': (LADDER_TO_RUNG_3, 2),
    'ladder_exhausted': (LADDER_EXHAUSTED, 2),
}

#: MAIN's (PR-1's) ordered broker calls of kind {place, cancel, get_order,
#: quote, stock_quote} per fixture — derived by running this class against
#: ``git show origin/main:src/strategy/call_roller.py`` with
#: FC120_PRINT_GOLDEN=1 (which prints instead of asserting). Kept as the
#: record FC-120 PR-2's goldens are diffed against (T-18). Its C380 limit is
#: main's book (8.50/8.70); the structure comparison ignores limits.
_O, _N, _F = OLD_SYMBOL, C375['symbol'], C380['symbol']
_PRELUDE = [('stock_quote', 'GOOGL'), ('quote', _O), ('quote', _N),
            ('place', (_O, 'buy', 8.4)), ('get_order', 'btc-1')]
MAIN_ORDER_PATH = {
    'btc_timeout': _PRELUDE + [('cancel', 'btc-1'), ('get_order', 'btc-1')],
    'full_fill': _PRELUDE + [('place', (_N, 'sell', 10.9)),
                             ('get_order', 'sto-1')],
    'ladder_exhausted': _PRELUDE + [
        ('place', (_N, 'sell', 10.9)), ('get_order', 'sto-1'),
        ('place', (_N, 'sell', 8.35)), ('get_order', 'sto-2'),
        ('quote', _F),
        ('place', (_F, 'sell', 8.5)), ('get_order', 'sto-3')],
    'ladder_to_rung_3': _PRELUDE + [
        ('place', (_N, 'sell', 10.9)), ('get_order', 'sto-1'),
        ('cancel', 'sto-1'), ('get_order', 'sto-1'),
        ('place', (_N, 'sell', 8.35)), ('get_order', 'sto-2'),
        ('quote', _F),
        ('cancel', 'sto-2'), ('get_order', 'sto-2'),
        ('place', (_F, 'sell', 8.5)), ('get_order', 'sto-3')],
}
MAIN_ORDER_PATH['btc_timeout_single'] = MAIN_ORDER_PATH['btc_timeout']

#: FC-120 PR-2's goldens (T-18), derived with FC120_PRINT_GOLDEN=1 and checked
#: by hand against DD-3/DD-4. The differences from main's, and ONLY these:
#:   * the three bounded execute-time PRICING reads (old option, stock, new
#:     option) replace main's one main-thread pre-BTC re-check read;
#:   * one bounded pricing read of the new symbol right after the BTC fill —
#:     rung 1's fresh basis (DD-3: ``min(pre_btc_bid, fresh_bid)``);
#:   * the fallback re-quote is the same read, now bounded (same position);
#:   * btc_timeout: three bounded pricing reads between attempts — each
#:     re-price, placed only after the roller's own cancel settled zero-fill;
#:   * the limits: buffered + snapped (BTC 8.40 -> 8.50, re-priced 8.60 / 8.70
#:     at (n+2) x buffer; rung 1 10.90 -> 10.80; C380 8.80 -> 8.70; the floor
#:     8.35 is the BTC fill + the fixture's $0.00 minimum, already on tick).
#: Place / poll / cancel / settle order and the order arguments' structure are
#: main's (asserted by test_the_structure_is_mains_but_for_the_pricing_reads).
_PRICED = [('pricing_quote', _O), ('pricing_stock_quote', 'GOOGL'),
           ('pricing_quote', _N)]
_EVAL = [('stock_quote', 'GOOGL'), ('quote', _O)]
PR2_ORDER_PATH = {
    'btc_timeout': _EVAL + _PRICED + [
        ('place', (_O, 'buy', 8.5)), ('get_order', 'btc-1'),
        ('cancel', 'btc-1'), ('get_order', 'btc-1')] + _PRICED + [
        ('place', (_O, 'buy', 8.6)), ('get_order', 'btc-2'),
        ('cancel', 'btc-2'), ('get_order', 'btc-2')] + _PRICED + [
        ('place', (_O, 'buy', 8.7)), ('get_order', 'btc-3'),
        ('cancel', 'btc-3'), ('get_order', 'btc-3')],
    'btc_timeout_single': _EVAL + _PRICED + [
        ('place', (_O, 'buy', 8.5)), ('get_order', 'btc-1'),
        ('cancel', 'btc-1'), ('get_order', 'btc-1')],
    'full_fill': _EVAL + _PRICED + [
        ('place', (_O, 'buy', 8.5)), ('get_order', 'btc-1'),
        ('pricing_quote', _N),
        ('place', (_N, 'sell', 10.8)), ('get_order', 'sto-1')],
    'ladder_exhausted': _EVAL + _PRICED + [
        ('place', (_O, 'buy', 8.5)), ('get_order', 'btc-1'),
        ('pricing_quote', _N),
        ('place', (_N, 'sell', 10.8)), ('get_order', 'sto-1'),
        ('place', (_N, 'sell', 8.35)), ('get_order', 'sto-2'),
        ('pricing_quote', _F),
        ('place', (_F, 'sell', 8.7)), ('get_order', 'sto-3')],
    'ladder_to_rung_3': _EVAL + _PRICED + [
        ('place', (_O, 'buy', 8.5)), ('get_order', 'btc-1'),
        ('pricing_quote', _N),
        ('place', (_N, 'sell', 10.8)), ('get_order', 'sto-1'),
        ('cancel', 'sto-1'), ('get_order', 'sto-1'),
        ('place', (_N, 'sell', 8.35)), ('get_order', 'sto-2'),
        ('pricing_quote', _F),
        ('cancel', 'sto-2'), ('get_order', 'sto-2'),
        ('place', (_F, 'sell', 8.7)), ('get_order', 'sto-3')],
}


def _structure(path, *, drop_main_thread_quotes_after_eval=False):
    """An order path with the pricing reads and the limits taken out: what is
    left — places (symbol, side), polls, cancels, settles and their order —
    is what PR-2 must keep identical to main."""
    out = []
    seen_place = False
    for i, (kind, key) in enumerate(path):
        if kind.startswith('pricing_'):
            continue
        if kind == 'place':
            seen_place = True
            out.append((kind, key[:2]))
            continue
        # Main's pre-BTC re-check (the third prelude entry) and its fallback
        # re-quotes were MAIN-THREAD pricing reads; PR-2 bounds them.
        if drop_main_thread_quotes_after_eval and kind == 'quote' and (
                seen_place or i >= 2):
            continue
        out.append((kind, key))
    return out


class TestTheOrderPathIsMains:
    """Ruling A + 1(a): the ordered list of order-path broker calls is
    identical to main's, and no diagnostic read sits (i) between the pre-BTC
    re-check and the BTC placement, (ii) between a BTC fill and an STC
    placement, (iii) between a settle and the next rung's placement, or (iv)
    between a final disposition and its terminal event.

    The structural rule checked: walking back from any ``diag`` read over
    other ``diag`` reads and non-terminal log events, the first thing found is
    an order PLACEMENT or a TERMINAL event. Each forbidden window above ends
    in something else (a quote, a poll read), so a diag read inside one fails.
    *Mutation:* move the BTC re-read back above ``place_option_order`` → (i)
    fails; take the post-settle quote before ``call_roll_btc_timeout_canceled``
    → (iv) fails; flush the rung's post-settle work before the next placement
    → (iii) fails.

    FC-120 PR-2 (T-18): the goldens are re-pinned (``PR2_ORDER_PATH``) and
    their STRUCTURE is asserted equal to main's but for the bounded pricing
    reads; the pricing reads are order-path reads (``pricing_*``), never diag.
    """

    @staticmethod
    def _run(roller, instrumented, name):
        script, attempts = ORDER_PATH_FIXTURES[name]
        roller.config.rolling_btc_reprice_attempts = attempts
        return _drive_timeline(roller, instrumented, script)

    @pytest.mark.parametrize('name', sorted(ORDER_PATH_FIXTURES))
    def test_order_path_is_mains(self, roller, instrumented, name):
        _r, _log, timeline = self._run(roller, instrumented, name)
        if os.environ.get('FC120_PRINT_GOLDEN'):
            print(f"\nGOLDEN {name}: {timeline.order_path()!r}")
            return
        assert timeline.order_path() == PR2_ORDER_PATH[name]

    @pytest.mark.parametrize('name', sorted(set(ORDER_PATH_FIXTURES) - {'btc_timeout'}))
    def test_the_structure_is_mains_but_for_the_pricing_reads(
            self, roller, instrumented, name):
        """Every single-attempt scenario: drop the pricing reads and the limits
        from PR-2's golden and from main's, and what is left — every place,
        poll, cancel and settle, in order — is identical. (The 3-attempt
        ``btc_timeout`` differs from main exactly by its two re-priced
        attempts, pinned in full above.)"""
        assert _structure(PR2_ORDER_PATH[name]) == _structure(
            MAIN_ORDER_PATH[name], drop_main_thread_quotes_after_eval=True)

    @pytest.mark.parametrize('name', sorted(ORDER_PATH_FIXTURES))
    def test_no_diag_read_inside_a_forbidden_window(self, roller, instrumented,
                                                    name):
        if os.environ.get('FC120_PRINT_GOLDEN'):
            pytest.skip('golden derivation run')
        _r, _log, timeline = self._run(roller, instrumented, name)
        entries = timeline.entries
        assert any(kind == 'diag' for kind, _ in entries), entries
        for i, (kind, _key) in enumerate(entries):
            if kind != 'diag':
                continue
            j = i - 1
            while j >= 0 and (entries[j][0] == 'diag' or (
                    entries[j][0] == 'log'
                    and entries[j][1] not in TERMINAL_EVENTS)):
                j -= 1
            anchor = entries[j] if j >= 0 else None
            assert anchor is not None and (
                anchor[0] == 'place'
                or (anchor[0] == 'log' and anchor[1] in TERMINAL_EVENTS)), (
                f"diag read at {i} follows {anchor}: {entries}")


class TestQuoteFieldsOnRollEvents:
    """T-1. Every placement carries the quote it was priced from; the re-read
    (taken after the order is out — ruling A) rides on the leg's later rows.

    *Catches:* a field on one leg only; an age computed from our clock instead
    of the broker stamp; a datetime reaching the logger; ``Decimal(float)`` in
    ``limit_on_tick`` (a 6.95 limit reading False).
    """

    def test_the_btc_placement_carries_the_dd2_set(self, roller, instrumented):
        _result, log = _drive(roller, instrumented, FULL_FILL)
        btc = _first(log, 'call_roll_btc_placed')

        assert btc['leg'] == 'btc' and btc['attempt'] == 0
        assert btc['pricing_mode'] == 'base'
        # FC-120 PR-2: the execute-time ask + the 0.10 buffer, on the grid.
        assert btc['limit_price'] == 8.50
        assert btc['quote_bid'] == 8.00 and btc['quote_ask'] == 8.40
        assert btc['quote_mid'] == 8.20 and btc['quote_spread'] == 0.40
        assert btc['quote_bid_size'] == 10 and btc['quote_ask_size'] == 12
        assert btc['quote_feed'] == 'indicative'
        assert isinstance(btc['quote_ts'], str)
        # The BROKER's stamp, ~40 s old — not 0 from our own clock.
        assert btc['quote_age_s'] == pytest.approx(40, abs=3)
        assert btc['stock_bid'] == 376.90 and btc['stock_ask'] == 377.10
        assert isinstance(btc['stock_quote_ts'], str)
        assert btc['stock_bid_ts'] == btc['stock_quote_ts']
        assert btc['stock_quote_age_s'] == pytest.approx(1, abs=3)
        assert btc['intrinsic_at_placement'] == 6.90  # 376.90 - 370
        assert btc['limit_minus_intrinsic'] == 1.60
        assert btc['limit_on_tick'] is True           # 8.50 on the nickel grid
        # PR-1's meaning kept: how far through the quoted ask — now >= buffer.
        assert btc['limit_vs_quote'] == 0.10
        # PR-2's log-only provenance (R6-R): which reads priced it.
        assert btc['btc_quote_source'] == 'execution'
        assert btc['stock_quote_source'] == 'execution'
        assert btc['parity_floor_applied'] is False   # 6.91 < 8.50
        assert btc['parity_stock_bid'] == 376.90
        assert btc['limit_formula'] == 'base'
        assert isinstance(btc['placed_at'], str)
        assert isinstance(btc['roll_id'], str) and len(btc['roll_id']) == 32
        # The re-read is NOT read yet when this event is written (ruling A).
        assert 'quote_reread_ask' not in btc

    def test_the_btc_reread_rides_on_the_fill_and_settled_rows(
            self, roller, instrumented):
        _result, log = _drive(roller, instrumented, FULL_FILL)
        for row in (_first(log, 'call_roll_btc_filled'),
                    _all(log, 'call_roll_leg_settled')[0]):
            assert row['quote_reread_ask'] == 8.42
            assert row['quote_reread_delta'] == 0.02   # reread_ask - ask
            assert isinstance(row['quote_reread_ts'], str)
            # Distinct broker stamps on the two reads.
            assert row['quote_reread_same_tick'] is False

    def test_same_tick_is_true_only_for_the_same_stored_quote(self):
        stamp = _stamp(5.0)
        same = cr._reread_fields(_q(8.00, 8.40, stamp=stamp),
                                 _q(8.00, 8.40, stamp=stamp), 'buy')
        assert same['quote_reread_same_tick'] is True
        assert same['quote_reread_delta'] == 0.0
        moved = cr._reread_fields(_q(8.00, 8.40, stamp=stamp),
                                  _q(8.00, 8.40, stamp=_stamp(1.0)), 'buy')
        assert moved['quote_reread_same_tick'] is False
        unknown = cr._reread_fields(_q(8.00, 8.40, stamp=None),
                                    _q(8.00, 8.40, stamp=stamp), 'buy')
        assert unknown['quote_reread_same_tick'] is None

    def test_stc_rung_1_carries_its_own_quote_and_the_chain_age(
            self, roller, instrumented):
        _result, log = _drive(roller, instrumented, FULL_FILL)
        stc = _first(log, 'call_roll_stc_placed')

        assert stc['leg'] == 'stc' and stc['rung'] == 1
        assert stc['pricing_mode'] == 'base'
        # FC-120 PR-2: min(pre-BTC 10.90, fresh 10.90) - 0.10.
        assert stc['limit_price'] == 10.80
        assert stc['quote_bid'] == 10.90 and stc['quote_ask'] == 11.10
        assert stc['limit_vs_quote'] == 0.10         # through the bid by the buffer
        assert stc['quote_age_s'] == pytest.approx(40, abs=3)
        assert stc['chain_quote_age_s'] == pytest.approx(100, abs=3)
        assert stc['chain_quote_feed'] is None       # the chain call names none
        assert stc['quote_reused_from_rung'] is None
        assert stc['intrinsic_at_placement'] == 1.90  # 376.90 - 375
        assert stc['limit_minus_intrinsic'] == 8.90
        # PR-2's ladder fields (DD-3).
        assert stc['rung_kind'] == 'primary' and stc['escalation_index'] == 0
        assert stc['basis_bid'] == 10.90 and stc['stc_quote_source'] == 'fresh'
        assert stc['pre_btc_bid'] == 10.90
        assert stc['pre_btc_quote_source'] == 'execution'
        assert stc['primary_limit'] == 10.80 and stc['limit_formula'] == 'base'
        assert 'prior_rung_limit' not in stc       # the first rung has no prior
        # The roll key, on every STO row (T-MEDIUM-4).
        btc = _first(log, 'call_roll_btc_placed')
        assert stc['roll_id'] == btc['roll_id']
        assert stc['old_option_symbol'] == OLD_SYMBOL
        assert stc['btc_order_id'] == 'btc-1'
        filled = _first(log, 'call_roll_stc_filled')
        assert filled['quote_reread_bid'] == 10.92
        assert filled['quote_reread_delta'] == -0.02  # bid - reread_bid

    def test_rungs_2_and_3_carry_their_quotes(self, roller, instrumented):
        _result, log = _drive(roller, instrumented, LADDER_TO_RUNG_3)
        placed = _all(log, 'call_roll_stc_placed')
        assert [p['rung'] for p in placed] == [1, 2, 3]

        rung2, rung3 = placed[1], placed[2]
        # Rung 2 has no quote of its own: rung 1's, flagged as reused.
        assert rung2['quote_reused_from_rung'] == 1
        assert rung2['quote_bid'] == 10.90
        assert rung2['limit_price'] == 8.35          # btc fill + $0.00 floor
        assert rung2['limit_vs_quote'] == 2.55       # 10.90 - 8.35
        assert rung2['rung_kind'] == 'floor' and rung2['limit_formula'] is None
        assert rung2['prior_rung_limit'] == 10.80
        assert rung2['prior_rung_disposition'] == 'timeout_canceled'
        # Rung 3 is the C380 fallback, priced from its own per-rung read.
        assert rung3['quote_bid'] == 8.80
        assert rung3['limit_price'] == 8.70          # 8.80 - the buffer
        assert rung3['intrinsic_at_placement'] == 0.0  # OTM: 376.90 < 380
        assert rung3['limit_minus_intrinsic'] == 8.70
        assert rung3['quote_age_s'] == pytest.approx(40, abs=3)
        assert rung3['rung_kind'] == 'fallback'
        rows = {r['rung']: r for r in _all(log, 'call_roll_leg_settled')
                if r['leg'] == 'stc'}
        # Rung 2 reuses rung 1's re-read too; rung 3 has its own.
        assert rows[2]['quote_reread_bid'] == rows[1]['quote_reread_bid'] == 10.92
        assert rows[3]['quote_reread_bid'] == 8.82
        assert rows[3]['quote_reread_delta'] == -0.02

    def test_the_dry_run_carries_both_legs_quote_sets(
            self, roller, instrumented, rolling_config):
        rolling_config.roller_dry_run = True
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())
        with patch('src.strategy.call_roller.logger') as log:
            roller.execute_roll(opp)
        dry = _first(log, 'call_roll_dry_run')

        # FC-120 PR-2: the dry run returns AFTER execute-time pricing, so it
        # carries the limits that would be placed and the quotes behind them.
        assert dry['would_be_btc_limit'] == 8.50
        assert dry['quote_ask'] == 8.40
        assert dry['quote_age_s'] == pytest.approx(40, abs=3)
        assert dry['limit_on_tick'] is True and dry['stock_bid'] == 376.90
        assert dry['btc_quote_source'] == 'execution'
        # The STO rung-1 set: the execute-time read of the new symbol.
        assert dry['stc_quote_bid'] == 10.90 and dry['stc_quote_ask'] == 11.10
        assert dry['stc_limit_vs_quote'] == 0.10
        assert dry['stc_quote_age_s'] == pytest.approx(40, abs=3)
        assert dry['stc_quote_feed'] == 'indicative'
        assert dry['stc_quote_source'] == 'execution'
        assert dry['stc_intrinsic_at_placement'] == 1.90
        instrumented.place_option_order.assert_not_called()
        # Evaluation's read plus the execute-time old + new option reads;
        # nothing diagnostic (no order exists to diagnose).
        assert instrumented.get_option_quote.call_count == 3

    @pytest.mark.parametrize('limit,underlying,expected', [
        (6.94, 'GOOGL', False),   # off the nickel grid above $3.00
        (6.95, 'GOOGL', True),    # Decimal(6.95) % 0.05 == 1.78e-16: _dec only
        (2.98, 'GOOGL', True),    # penny below $3.00
        (11.71, 'GOOGL', False),  # the 09-21 CC limit that timed out
        (2.98, 'IWM', True),      # always-penny
        (6.94, 'IWM', True),      # always-penny at every price
        (0, 'GOOGL', None),       # no limit -> no verdict, never False
    ])
    def test_limit_on_tick(self, limit, underlying, expected):
        assert cr._limit_on_tick(limit, underlying) is expected

    @pytest.mark.parametrize('name', ['full', 'ladder', 'timeout', 'exhausted'])
    def test_every_new_field_is_a_json_primitive(self, roller, instrumented,
                                                 name):
        """S8: the broker stamp is a datetime; it must reach the log as a
        string. Checked on every instrumented event of each shape."""
        script = {'full': FULL_FILL, 'ladder': LADDER_TO_RUNG_3,
                  'timeout': BTC_TIMEOUT, 'exhausted': LADDER_EXHAUSTED}[name]
        with patch('src.strategy.call_roller.logger') as log:
            opp = roller.evaluate_roll_opportunity(call_position(),
                                                   stock_position())
        instrumented.get_option_quote.side_effect = _book()   # fresh counters
        _result, log2 = _drive(roller, instrumented, script)
        seen = 0
        for event_type, kwargs in events(log) + events(log2):
            if event_type not in INSTRUMENTED_EVENTS:
                continue
            seen += 1
            bad = {k: type(v).__name__ for k, v in kwargs.items()
                   if not isinstance(v, PRIMITIVES)}
            assert not bad, f"{event_type} carries non-primitives: {bad}"
        assert seen >= 3, event_types(log2)
        assert opp is not None

    def test_one_roll_id_per_execute_roll_call(self, roller, instrumented):
        _r, log_a = _drive(roller, instrumented, LADDER_TO_RUNG_3)
        ids_a = {kw['roll_id'] for e, kw in events(log_a)
                 if e in ('call_roll_btc_placed', 'call_roll_stc_placed',
                          'call_roll_leg_settled', 'call_roll_stc_filled',
                          'call_roll_btc_filled',
                          'call_roll_stc_timeout_canceled')}
        assert len(ids_a) == 1 and None not in ids_a
        instrumented.reset_mock()
        instrumented.get_option_quote.side_effect = _book()
        _r, log_b = _drive(roller, instrumented, FULL_FILL)
        assert _first(log_b, 'call_roll_btc_placed')['roll_id'] not in ids_a

    def test_stock_age_is_computed_at_placement_from_the_stored_stamp(
            self, roller, instrumented):
        """T-MEDIUM-3: same time base as ``quote_age_s`` — the stamp is
        stored on the opportunity and aged when the row is written."""
        instrumented.get_stock_quote.return_value = {
            'bid': 376.90, 'ask': 377.10, 'timestamp': _stamp(30.0)}
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())
        assert 'stock_quote_age_s' not in opp           # a stamp, not an age
        opp['stock_quote_ts'] = _stamp(90.0).isoformat()   # "time passed"
        instrumented.place_option_order.side_effect = [accepted('btc-1'),
                                                       accepted('sto-1')]
        broker = _Broker(FULL_FILL)
        instrumented.get_order_by_id.side_effect = broker.by_id
        with patch('src.strategy.call_roller.logger') as log:
            roller.execute_roll(opp)
        assert _first(log, 'call_roll_btc_placed')['stock_quote_age_s'] == \
            pytest.approx(90, abs=3)


class TestTheEvaluationQuoteSetOnSkips:
    """T-MEDIUM-7 / S-MEDIUM-4: ``call_roll_evaluated`` and every skip AFTER the
    BTC quote read carry that quote and the IEX stock quote — with NO extra
    read. *Catches:* the cycles that never place an order logging no quote."""

    def _eval(self, roller, instrumented):
        with patch('src.strategy.call_roller.logger') as log:
            opp = roller.evaluate_roll_opportunity(call_position(),
                                                   stock_position())
        return opp, log

    def test_evaluated_carries_the_btc_set_without_a_reread(self, roller,
                                                           instrumented):
        _opp, log = self._eval(roller, instrumented)
        ev = _first(log, 'call_roll_evaluated')
        assert ev['quote_ask'] == 8.40 and ev['quote_bid'] == 8.00
        assert ev['quote_age_s'] == pytest.approx(40, abs=3)
        assert ev['stock_bid'] == 376.90 and isinstance(ev['stock_bid_ts'], str)
        assert ev['intrinsic_at_placement'] == 6.90
        assert ev['limit_on_tick'] is True
        assert 'quote_reread_ask' not in ev
        assert instrumented.get_option_quote.call_count == 1

    def test_no_credit_candidate_carries_it(self, roller, instrumented,
                                            mock_market_data):
        mock_market_data.find_suitable_calls.return_value = [
            dict(C375, bid=1.00, ask=1.20)]
        opp, log = self._eval(roller, instrumented)
        assert opp is None
        skip = _first(log, 'call_roll_skipped')
        assert skip['skip_reason'] == 'no_credit_candidate'
        assert skip['quote_ask'] == 8.40 and skip['stock_bid'] == 376.90

    def test_no_suitable_replacement_carries_it(self, roller, instrumented,
                                                mock_market_data):
        mock_market_data.find_suitable_calls.return_value = []
        _opp, log = self._eval(roller, instrumented)
        skip = _first(log, 'call_roll_skipped')
        assert skip['skip_reason'] == 'no_suitable_replacement'
        # The screening limit is buffered (FC-120 PR-2): 8.50 - 6.90.
        assert skip['quote_ask'] == 8.40 and skip['limit_minus_intrinsic'] == 1.60

    def test_earnings_unknown_carries_it(self, roller, instrumented,
                                         mock_earnings):
        mock_earnings.next_earnings_info.return_value = ('unknown', None)
        _opp, log = self._eval(roller, instrumented)
        skip = _first(log, 'call_roll_skipped')
        assert skip['skip_reason'] == 'earnings_unknown'
        assert skip['quote_ask'] == 8.40

    def test_credit_gone_at_execution_carries_it(self, roller, instrumented):
        opp, _log = self._eval(roller, instrumented)
        instrumented.get_option_quote.side_effect = _book(**{
            C375['symbol']: [_q(1.00, 1.20)]})
        with patch('src.strategy.call_roller.logger') as log:
            result = roller.execute_roll(opp)
        assert result['reason'] == 'credit_gone_at_execution'
        skip = _first(log, 'call_roll_skipped')
        assert skip['quote_ask'] == 8.40 and skip['stock_bid'] == 376.90
        instrumented.place_option_order.assert_not_called()

    def test_skip_reasons_are_remembered_for_the_sampler(self, roller,
                                                         instrumented,
                                                         mock_market_data):
        mock_market_data.find_suitable_calls.return_value = []
        self._eval(roller, instrumented)
        assert roller.skip_reasons == {OLD_SYMBOL: 'no_suitable_replacement'}


class TestMissingQuoteDataLogsNone:
    """T-2. A stamp-less quote logs None — never 0.0, never a raise — and a
    failing re-read logs ``quote_reread_delta=None`` with the disposition
    unchanged."""

    @pytest.mark.parametrize('stamp', [None, 'not-a-time', '', 12345])
    def test_an_unreadable_stamp_is_none(self, stamp):
        fields = cr._quote_fields(_q(8.00, 8.40, stamp=stamp), 8.40, 'buy',
                                  'GOOGL', 370.0, 376.90)
        assert fields['quote_ts'] is None
        assert fields['quote_age_s'] is None
        reread = cr._reread_fields(_q(8.00, 8.40, stamp=stamp), None, 'buy')
        assert reread['quote_reread_delta'] is None
        assert reread['quote_reread_same_tick'] is None

    def test_an_absent_quote_is_all_none_not_zero(self):
        fields = cr._quote_fields({}, 8.40, 'buy', 'GOOGL', 370.0, None)
        for key in ('quote_bid', 'quote_ask', 'quote_mid', 'quote_spread',
                    'limit_vs_quote', 'intrinsic_at_placement',
                    'limit_minus_intrinsic'):
            assert fields[key] is None, key
        assert fields['limit_on_tick'] is True   # the limit itself is known

    def test_a_raising_reread_changes_nothing_but_the_field(
            self, roller, instrumented):
        boom = RuntimeError("quote endpoint down")
        instrumented.get_option_quote.side_effect = _book(**{
            OLD_SYMBOL: [_q(8.00, 8.40), boom, _q(8.00, 8.40)],
            C375['symbol']: [_q(10.90, 11.10), boom, _q(10.90, 11.10)]})

        result, log = _drive(roller, instrumented, FULL_FILL)

        assert result['success'] is True
        assert terminals(log) == ['call_roll_completed']
        for event_type in ('call_roll_btc_filled', 'call_roll_stc_filled'):
            kw = _first(log, event_type)
            assert kw['quote_reread_delta'] is None, event_type
            assert kw['quote_reread_ask'] is None, event_type
        # The limits are the priced reads', exactly as without the re-read
        # (FC-120 PR-2's buffered ones).
        assert [c.kwargs['limit_price'] for c in
                instrumented.place_option_order.call_args_list] == [8.50, 10.80]


class TestTheTimeoutPathReadsAfterTheTerminal:
    """T-3 under ruling A. The terminal goes out first with the fields in
    hand; one option quote and one stock quote follow it; their fields ride on
    the leg_settled row ONLY. *Catches:* a refresh before the settle or before
    the terminal; ``polls`` overwritten by the settle's re-entry.

    FC-120 PR-2: this is the timeout of the LAST attempt — the only one that
    reads its own post-settle option quote — so re-pricing is off here (one
    attempt). A re-priced attempt's cancel quote is the re-price read (T-10(k)).
    """

    @pytest.fixture(autouse=True)
    def _one_attempt(self, roller):
        roller.config.rolling_btc_reprice_attempts = 0

    def test_call_and_event_order(self, roller, instrumented):
        result, _log, timeline = _drive_timeline(roller, instrumented,
                                                 BTC_TIMEOUT)
        assert result['reason'] == 'btc_timeout_canceled'
        tail = [e for e in timeline.entries
                if e[0] != 'log' or e[1] in ('call_roll_btc_timeout_canceled',
                                             'call_roll_leg_settled')]
        assert tail[-9:] == [
            ('place', (OLD_SYMBOL, 'buy', 8.5)),
            ('diag', OLD_SYMBOL),                   # the re-read, AFTER placing
            ('get_order', 'btc-1'),                 # the poll
            ('cancel', 'btc-1'),
            ('get_order', 'btc-1'),                 # the settle
            ('log', 'call_roll_btc_timeout_canceled'),   # terminal FIRST
            ('diag', OLD_SYMBOL),                   # then the post-settle quote
            ('diag', 'GOOGL'),                      # and the cancel-time stock
            ('log', 'call_roll_leg_settled'),
        ]

    def test_quote_drift_sign_and_the_cancel_set(self, roller, instrumented):
        # Extrinsic 7.34 - 7.00 = 0.34 > 0.20 keeps this in base mode.
        instrumented.get_option_quote.side_effect = _book(**{
            OLD_SYMBOL: [_q(7.30, 7.38), _q(7.30, 7.38), _q(7.40, 7.45)]})
        _result, log = _drive(roller, instrumented, BTC_TIMEOUT)
        ev = _first(log, 'call_roll_btc_timeout_canceled')
        assert ev['disposition'] == 'timeout_canceled'
        # FC-120 PR-2: 7.38 + 0.10 = 7.48, snapped UP to the nickel grid.
        assert ev['pricing_mode'] == 'base' and ev['limit_price'] == 7.50
        assert ev['order_status'] == 'canceled'
        assert ev['order_submitted_at'] == '2026-09-28T19:30:00+00:00'
        assert isinstance(ev['leg_elapsed_s'], float)
        # PR-1 logged this limit off-grid (7.38); PR-2's are on tick by
        # construction — the off-grid case is in TestQuoteFieldsOnRollEvents.
        assert ev['limit_on_tick'] is True
        assert ev['attempts'] == 1
        # Ruling A: no post-settle field on the terminal itself.
        assert 'quote_drift' not in ev and 'cancel_quote_ask' not in ev

        row = _first(log, 'call_roll_leg_settled')
        assert row['quote_ask'] == 7.38 and row['cancel_quote_ask'] == 7.45
        assert row['cancel_quote_bid'] == 7.40
        assert row['quote_drift'] == 0.07     # positive: market moved away
        assert isinstance(row['cancel_quote_ts'], str)
        assert row['cancel_quote_age_s'] == pytest.approx(40, abs=3)
        assert row['cancel_stock_bid'] == 376.90
        assert isinstance(row['cancel_stock_quote_ts'], str)
        assert row['order_status'] == 'canceled'
        assert terminals(log) == ['call_roll_btc_timeout_canceled']

    def test_a_raising_refresh_is_a_trade_breadcrumb_not_a_raise(
            self, roller, instrumented):
        instrumented.get_option_quote.side_effect = _book(**{
            OLD_SYMBOL: [_q(8.00, 8.40), _q(8.00, 8.40),
                         RuntimeError("quote endpoint down")]})
        result, log = _drive(roller, instrumented, BTC_TIMEOUT)

        assert result['reason'] == 'btc_timeout_canceled'
        assert terminals(log) == ['call_roll_btc_timeout_canceled']
        row = _first(log, 'call_roll_leg_settled')
        for key in ('cancel_quote_bid', 'cancel_quote_ask', 'cancel_quote_ts',
                    'cancel_quote_age_s', 'quote_drift'):
            assert row[key] is None, key
        crumbs = _all(log, 'call_roll_quote_refresh_failed')
        assert len(crumbs) == 1 and crumbs[0]['reason'] == 'error'
        # On the TRADE logger, never the error logger (T-LOW: errors_all).
        assert crumbs[0]['event_category'] == 'trade'
        assert not [c for c in log.error.call_args_list
                    if c.kwargs.get('event_type')
                    == 'call_roll_quote_refresh_failed']

    def test_polls_is_the_primary_poll_not_primary_plus_settle(
            self, roller, instrumented, monkeypatch):
        monkeypatch.setattr(call_roller_module,
                            '_CANCEL_SETTLE_TIMEOUT_SECONDS', 5)
        script = {'btc-1': {
            'poll': _o('btc-1', 'new'),
            'after_cancel': [_o('btc-1', 'pending_cancel'),
                             _o('btc-1', 'canceled')]}}
        _result, log = _drive(roller, instrumented, script)

        for event_type in ('call_roll_btc_timeout_canceled',
                           'call_roll_leg_settled'):
            ev = _first(log, event_type)
            assert ev['polls'] == 1, event_type          # the primary poll
            assert ev['settle_polls'] == 2, event_type   # the settle's own


#: scenario -> (broker script, expected (leg, rung, disposition) per row,
#:              the one terminal)
LEG_SCENARIOS = {
    'btc_fill_then_rung1_fill': (
        FULL_FILL,
        [('btc', None, 'filled'), ('stc', 1, 'filled')],
        'call_roll_completed'),
    # FC-120 PR-2: two re-prices, so three placed attempts — three rows.
    'btc_timeout': (
        BTC_TIMEOUT, [('btc', None, 'timeout_canceled')] * 3,
        'call_roll_btc_timeout_canceled'),
    'rung1_timeout_then_rung2_fill': (
        {'btc-1': BTC_FILLED, 'sto-1': _times_out('sto-1'),
         'sto-2': {'poll': _o('sto-2', 'filled', 1, 8.40, latency_s=2)}},
        [('btc', None, 'filled'), ('stc', 1, 'timeout_canceled'),
         ('stc', 2, 'filled')],
        'call_roll_completed'),
    'ladder_exhausted': (
        LADDER_EXHAUSTED,
        [('btc', None, 'filled'), ('stc', 1, 'terminal_no_fill'),
         ('stc', 2, 'terminal_no_fill'), ('stc', 3, 'terminal_no_fill')],
        'call_roll_naked_exposure'),
    'all_rungs_time_out': (
        ALL_RUNGS_TIME_OUT,
        [('btc', None, 'filled'), ('stc', 1, 'timeout_canceled'),
         ('stc', 2, 'timeout_canceled'), ('stc', 3, 'timeout_canceled')],
        'call_roll_naked_exposure'),
    'btc_unknown_disposition': (
        {'btc-1': {'poll': _o('btc-1', 'new'),
                   'after_cancel': _o('btc-1', 'pending_cancel')}},
        [('btc', None, 'unknown')], 'call_roll_unknown_disposition'),
    'btc_rejected_after_placement': (
        {'btc-1': {'poll': _o('btc-1', 'rejected')}},
        [('btc', None, 'rejected')], 'call_roll_btc_rejected'),
    'stc_unknown_disposition': (
        {'btc-1': BTC_FILLED,
         'sto-1': {'poll': _o('sto-1', 'new'),
                   'after_cancel': _o('sto-1', 'pending_cancel')}},
        [('btc', None, 'filled'), ('stc', 1, 'unknown')],
        'call_roll_unknown_disposition'),
    # The settle shows the order EXPIRED, not canceled by us: the true
    # disposition from order_status (T-LOW), not "timeout_canceled".
    'rung1_settles_expired': (
        {'btc-1': BTC_FILLED,
         'sto-1': {'poll': _o('sto-1', 'new'),
                   'after_cancel': _o('sto-1', 'expired')},
         'sto-2': {'poll': _o('sto-2', 'filled', 1, 8.40, latency_s=2)}},
        [('btc', None, 'filled'), ('stc', 1, 'terminal_no_fill'),
         ('stc', 2, 'filled')],
        'call_roll_completed'),
}


class TestOneSettledRowPerPlacedOrder:
    """T-4 + T-5. ``call_roll_leg_settled`` is exactly one row per placed
    order, at its disposition, and the one-terminal-per-position contract
    still holds with it present. *Catches:* a row per poll; a missing row on
    ``_settle_live_rung``'s zero-fill branch; STC rows lacking ``rung``."""

    @pytest.mark.parametrize('name', sorted(LEG_SCENARIOS))
    def test_rows_and_terminal(self, roller, instrumented, name):
        script, expected_rows, terminal = LEG_SCENARIOS[name]
        _result, log = _drive(roller, instrumented, script)

        rows = _all(log, 'call_roll_leg_settled')
        assert sorted((r['leg'], r.get('rung') or 0, r['disposition'])
                      for r in rows) == sorted(
            (leg, rung or 0, d) for leg, rung, d in expected_rows)
        assert len(rows) == instrumented.place_option_order.call_count
        assert terminals(log) == [terminal]          # T-5: still exactly one
        roll_ids = {row['roll_id'] for row in rows}
        assert len(roll_ids) == 1 and None not in roll_ids
        for row in rows:
            assert row['strategy'] == 'roll_call'
            assert row['requested_qty'] == 1
            assert row['limit_price'] > 0
            if row['disposition'] in ('filled', 'partial'):
                assert row['fill_vs_limit'] >= 0
                assert row['cancel_quote_ask'] is None   # none on fills
            if row['disposition'] in ('timeout_canceled', 'terminal_no_fill'):
                assert row['cancel_quote_ask'] is not None
                assert row['order_status'] in ('canceled', 'expired')
            if row['leg'] == 'stc':
                assert row['old_option_symbol'] == OLD_SYMBOL
                assert row['btc_order_id'] == 'btc-1'
            assert isinstance(row['leg_elapsed_s'], float)

    def test_fill_rows_carry_latency_and_fill_quality(self, roller,
                                                      instrumented):
        _result, log = _drive(roller, instrumented, FULL_FILL)
        btc, stc = _all(log, 'call_roll_leg_settled')

        # FC-120 PR-2: the limits sit a buffer through the quote, so a fill at
        # the same NBBO price reads 0.10 further through them.
        assert btc['fill_vs_limit'] == 0.15      # limit 8.50 - fill 8.35
        assert btc['fill_price_source'] == 'broker'
        assert btc['fill_latency_s'] == 5.0      # filled_at - submitted_at
        assert btc['filled_price'] == 8.35 and btc['filled_qty'] == 1
        assert btc['polls'] == 1 and btc['settle_polls'] is None
        assert btc['order_status'] == 'filled'
        assert stc['fill_vs_limit'] == 0.15      # fill 10.95 - limit 10.80
        assert stc['fill_latency_s'] == 4.0
        assert stc['order_id'] == 'sto-1' and btc['order_id'] == 'btc-1'
        filled = _first(log, 'call_roll_btc_filled')
        assert filled['limit_price'] == 8.50 and filled['fill_vs_limit'] == 0.15
        assert filled['fill_latency_s'] == 5.0 and filled['polls'] == 1
        stc_filled = _first(log, 'call_roll_stc_filled')
        assert stc_filled['rung'] == 1 and stc_filled['fill_vs_limit'] == 0.15

    def test_an_absent_fill_price_is_a_fallback_not_a_measurement(
            self, roller, instrumented):
        """T-LOW-8: with no ``filled_avg_price`` the limit stands in for the
        fill; comparing a limit with itself would log a fake 0.00."""
        script = {'btc-1': {'poll': _o('btc-1', 'filled', 1, None)},
                  'sto-1': {'poll': _o('sto-1', 'filled', 1, None)}}
        result, log = _drive(roller, instrumented, script)
        assert result['success'] is True
        for row in _all(log, 'call_roll_leg_settled') + [
                _first(log, 'call_roll_btc_filled'),
                _first(log, 'call_roll_stc_filled')]:
            assert row['fill_vs_limit'] is None
            assert row['fill_price_source'] == 'fallback'

    def test_rung_2_fill_logs_rung_1s_miss_offset(self, roller, instrumented):
        """Ruling E: ``prior_rung_miss_offset = rung1_limit - rung2_fill``."""
        script, _rows, _t = LEG_SCENARIOS['rung1_timeout_then_rung2_fill']
        _result, log = _drive(roller, instrumented, script)
        filled = _first(log, 'call_roll_stc_filled')
        assert filled['rung'] == 2
        assert filled['prior_rung_miss_offset'] == 2.40   # 10.80 - 8.40
        # FC-120 PR-2: the DD-1 Rule B measurement, and the prior's disposition.
        assert filled['primary_miss_offset'] == 2.40
        assert filled['prior_rung_disposition'] == 'timeout_canceled'
        row = [r for r in _all(log, 'call_roll_leg_settled')
               if r.get('rung') == 2][0]
        assert row['prior_rung_miss_offset'] == 2.40
        assert row['primary_miss_offset'] == 2.40
        rung1 = [r for r in _all(log, 'call_roll_leg_settled')
                 if r.get('rung') == 1][0]
        assert 'prior_rung_miss_offset' not in rung1

    def test_a_synchronous_rejection_is_one_rejected_row(self, roller,
                                                         instrumented):
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())
        instrumented.place_option_order.side_effect = [
            {'success': False, 'error_message': 'refused'}]
        with patch('src.strategy.call_roller.logger') as log:
            roller.execute_roll(opp)

        rows = _all(log, 'call_roll_leg_settled')
        assert [(r['leg'], r['disposition'], r['order_id']) for r in rows] == [
            ('btc', 'rejected', None)]
        assert terminals(log) == ['call_roll_btc_rejected']
        # Nothing was live, so nothing was re-read: evaluation's read and the
        # two execute-time pricing reads (FC-120 PR-2) only.
        assert instrumented.get_option_quote.call_count == 3


class TestStcLegFieldsKillTheSurvivingMutants:
    """T-MEDIUM-5. The STO fixture's post-settle read DIFFERS from the base
    quote on both sides by different amounts, so each mutant reads a
    different number:

    - drift sign flipped (``cancel_bid - quote_bid``) -> -0.15, not +0.15
    - drift on the wrong side (asks: ``quote_ask - cancel_ask``) -> +0.05
    - ``polls`` overwritten by the settle's count -> 2, not 1
    - ``chain_quote_age_s`` on rungs other than 1 -> not None on rung 2/3
    """

    def _book(self):
        # C375: priced 10.90/11.10, re-read shifted, post-settle 10.75/11.05.
        return _book(**{C375['symbol']: [
            _q(10.90, 11.10), _q(10.92, 11.12), _q(10.75, 11.05)]})

    def test_stc_drift_sign_and_side(self, roller, instrumented):
        instrumented.get_option_quote.side_effect = self._book()
        script, _rows, _t = LEG_SCENARIOS['rung1_timeout_then_rung2_fill']
        _result, log = _drive(roller, instrumented, script)
        rung1 = [r for r in _all(log, 'call_roll_leg_settled')
                 if r.get('rung') == 1][0]
        assert rung1['cancel_quote_bid'] == 10.75
        assert rung1['cancel_quote_ask'] == 11.05
        assert rung1['quote_drift'] == 0.15     # quote_bid - cancel_bid
        ev = _first(log, 'call_roll_stc_timeout_canceled')
        assert 'quote_drift' not in ev           # ruling A: settled row only

    def test_stc_polls_is_the_primary_poll(self, roller, instrumented,
                                           monkeypatch):
        monkeypatch.setattr(call_roller_module,
                            '_CANCEL_SETTLE_TIMEOUT_SECONDS', 5)
        script = {'btc-1': BTC_FILLED,
                  'sto-1': {'poll': _o('sto-1', 'new'),
                            'after_cancel': [_o('sto-1', 'pending_cancel'),
                                             _o('sto-1', 'canceled')]},
                  'sto-2': {'poll': _o('sto-2', 'filled', 1, 8.40)}}
        _result, log = _drive(roller, instrumented, script)
        ev = _first(log, 'call_roll_stc_timeout_canceled')
        assert ev['polls'] == 1 and ev['settle_polls'] == 2
        rung1 = [r for r in _all(log, 'call_roll_leg_settled')
                 if r.get('rung') == 1][0]
        assert rung1['polls'] == 1 and rung1['settle_polls'] == 2

    def test_chain_age_is_rung_1_only(self, roller, instrumented):
        _result, log = _drive(roller, instrumented, LADDER_TO_RUNG_3)
        for event_type in ('call_roll_stc_placed', 'call_roll_leg_settled'):
            by_rung = {r['rung']: r for r in _all(log, event_type)
                       if r.get('leg') == 'stc'}
            assert by_rung[1]['chain_quote_age_s'] == pytest.approx(100, abs=3)
            assert by_rung[2]['chain_quote_age_s'] is None, event_type
            assert by_rung[3]['chain_quote_age_s'] is None, event_type


def _placements(mock_alpaca):
    return [dict(c.kwargs) for c in mock_alpaca.place_option_order.call_args_list]


def _order(symbol, side, limit):
    return {'symbol': symbol, 'qty': 1, 'side': side, 'order_type': 'limit',
            'limit_price': limit}


#: FC-120 PR-2: every limit buffered 0.10 through its quote and tick-snapped.
#: BTC 8.40 -> 8.50; rung 1 10.90 -> 10.80; the floor is the 8.35 BTC fill
#: plus the fixture's $0.00 minimum (on tick already); C380 8.80 -> 8.70.
_LADDER_3_ORDERS = [
    _order(OLD_SYMBOL, 'buy', 8.50), _order(C375['symbol'], 'sell', 10.80),
    _order(C375['symbol'], 'sell', 8.35), _order(C380['symbol'], 'sell', 8.70)]

#: scenario -> (old book, broker script, the order arguments PR-2 places)
GOLDEN_PLACEMENTS = {
    'full_fill': ((8.00, 8.40), FULL_FILL, [
        _order(OLD_SYMBOL, 'buy', 8.50), _order(C375['symbol'], 'sell', 10.80)]),
    'ladder_to_rung_3': ((8.00, 8.40), LADDER_TO_RUNG_3, _LADDER_3_ORDERS),
    'ladder_exhausted': ((8.00, 8.40), LADDER_EXHAUSTED, _LADDER_3_ORDERS),
    'all_rungs_time_out': ((8.00, 8.40), ALL_RUNGS_TIME_OUT, _LADDER_3_ORDERS),
    # Two re-prices after the roller's own zero-fill cancels: the ask (8.40)
    # plus (n+1) x 0.10 — 8.50, 8.60, 8.70 (DD-4).
    'btc_timeout': ((8.00, 8.40), BTC_TIMEOUT, [
        _order(OLD_SYMBOL, 'buy', 8.50), _order(OLD_SYMBOL, 'buy', 8.60),
        _order(OLD_SYMBOL, 'buy', 8.70)]),
    # Extrinsic 7.10 - 7.00 = 0.10 <= 0.20: imminence prices each leg's FIRST
    # placement at mid +/- 0.05 (no buffer; the parity floor 6.91 does not
    # bind) — 7.15 and 11.00 - 0.05 = 10.95, both already on tick.
    'imminence_full_fill': ((7.00, 7.20), FULL_FILL, [
        _order(OLD_SYMBOL, 'buy', 7.15), _order(C375['symbol'], 'sell', 10.95)]),
}


def _diag_garbage(_name, _args, _real):
    """Every instrumentation read returns an absurd book."""
    return _q(0.01, 99.00, stamp=None)


def _diag_raises(_name, _args, _real):
    raise RuntimeError("instrumentation endpoint down")


class TestNoBehaviourChange:
    """FC-120's instrumentation changes NOTHING: same order arguments, same
    result dict, same terminals in the same order — when the instrumentation
    reads are clean, return garbage, raise, or never return. (PR-1 pinned this
    against main; PR-2 pins it against its own goldens, whose limits are the
    buffered, snapped ones.)

    Diagnostic reads are told apart by THREAD NAME (``_Timeline``): only
    ``fc120-diag-read`` workers are poisoned, so every pricing read — main
    thread or a PR-2 bounded ``fc120-pricing-read`` worker — is real.
    """

    def _clean(self, roller, instrumented, name):
        old, script, golden = GOLDEN_PLACEMENTS[name]
        instrumented.get_option_quote.side_effect = _book(old=old)
        result, log, _t = _drive_timeline(roller, instrumented, script)
        return result, terminals(log), golden

    @pytest.mark.parametrize('name', sorted(GOLDEN_PLACEMENTS))
    def test_order_arguments_match_main(self, roller, instrumented, name):
        _r, _t, golden = self._clean(roller, instrumented, name)
        assert _placements(instrumented) == golden

    @pytest.mark.parametrize('poison', ['garbage', 'raises'])
    @pytest.mark.parametrize('name', sorted(GOLDEN_PLACEMENTS))
    def test_poisoned_instrumentation_reads_change_nothing(
            self, roller, instrumented, name, poison):
        """Covers the STO ladder too (exhausted, all-rungs-timeout, rung-3
        fill) — S/T 1(c)."""
        clean_result, clean_terminals, golden = self._clean(
            roller, instrumented, name)
        instrumented.reset_mock()
        old, script, _g = GOLDEN_PLACEMENTS[name]
        instrumented.get_option_quote.side_effect = _book(old=old)
        hook = _diag_garbage if poison == 'garbage' else _diag_raises
        result, log, timeline = _drive_timeline(roller, instrumented, script,
                                                on_diag=hook)

        assert _placements(instrumented) == golden
        assert result == clean_result
        assert terminals(log) == clean_terminals
        assert any(kind == 'diag' for kind, _ in timeline.entries)

    @pytest.mark.parametrize('name', ['btc_timeout', 'ladder_exhausted',
                                      'all_rungs_time_out', 'ladder_to_rung_3'])
    def test_a_diagnostic_read_that_never_returns_delays_nothing(
            self, roller, instrumented, monkeypatch, name):
        """Ruling B / 1(b): every diag read blocks on an Event that is only
        set at teardown. The cap (shrunk to 50 ms here) abandons each one;
        the terminals and order arguments are main's, inside the bound."""
        monkeypatch.setattr(call_roller_module, '_DIAG_READ_TIMEOUT_SECONDS',
                            0.05)
        clean_result, clean_terminals, golden = self._clean(
            roller, instrumented, name)
        instrumented.reset_mock()
        old, script, _g = GOLDEN_PLACEMENTS[name]
        instrumented.get_option_quote.side_effect = _book(old=old)
        release = threading.Event()

        def hang(_name, _args, _real):
            release.wait(30)
            return _q(1.0, 2.0)
        try:
            started = time.monotonic()
            result, log, _t = _drive_timeline(roller, instrumented, script,
                                              on_diag=hang)
            wall = time.monotonic() - started
        finally:
            release.set()

        assert result == clean_result
        assert terminals(log) == clean_terminals
        assert _placements(instrumented) == golden
        assert wall < 5.0, wall
        # The rows still exist, with the unreadable fields null. (FC-120
        # PR-2: a re-priced BTC attempt's cancel quote IS the re-price's
        # bounded PRICING read — one read, two uses — which this test does
        # not hang; only the final attempt reads its own, and that one hung.)
        rows = _all(log, 'call_roll_leg_settled')
        assert len(rows) == len(golden)
        own_read = [r for r in rows
                    if not (r.get('leg') == 'btc' and r.get('attempt', 0) < 2
                            and name == 'btc_timeout')]
        assert all(r.get('cancel_quote_ask') is None for r in own_read)
        if name == 'btc_timeout':
            crumb = _first(log, 'call_roll_quote_refresh_failed')
            assert crumb['reason'] == 'timeout'
            prefetched = [r for r in rows if r.get('attempt', 0) < 2]
            assert all(r.get('cancel_quote_ask') == 8.40 for r in prefetched)


_JUNK = [10 ** 400, 1e308, -1e308, float('nan'), float('inf'),
         float('-inf'), 'junk', '', None, True, object(), [1], {'a': 1}]
_JUNK_STAMPS = ['not-a-time', '9999-99-99', 12345, '', object(),
                datetime(2026, 1, 1), _stamp(1.0)]


def _all_primitive_and_finite(fields):
    for key, value in fields.items():
        assert isinstance(value, PRIMITIVES), (key, value)
        if isinstance(value, float):
            assert value == value and value not in (float('inf'),
                                                    float('-inf')), key


class TestFieldBuildersAreTotal:
    """Ruling C / S-MEDIUM-6. Every builder survives absurd inputs — a huge
    int (``OverflowError``), a 1e308 limit (``decimal.InvalidOperation`` on
    the tick modulo), a non-string underlying, NaN/inf, junk stamps — and
    returns only finite JSON primitives or None."""

    @pytest.mark.parametrize('junk', _JUNK, ids=repr)
    def test_quote_and_reread_and_fill_fields(self, junk):
        quote = {'bid': junk, 'ask': junk, 'bid_size': junk, 'ask_size': junk,
                 'timestamp': junk, 'feed': junk}
        for underlying in ('GOOGL', 123, None):
            _all_primitive_and_finite(cr._evaluation_quote_set(
                quote, junk, underlying, junk, junk, junk, junk))
        _all_primitive_and_finite(cr._reread_fields(quote, quote, 'buy'))
        _all_primitive_and_finite(cr._reread_fields(junk, junk, 'sell'))
        _all_primitive_and_finite(CallRoller._fill_fields(
            limit=junk, filled_price=junk, side='buy',
            order={'filled_at': junk, 'submitted_at': junk},
            elapsed=junk, polls=junk, settle_polls=junk))
        assert cr._limit_on_tick(junk, 'GOOGL') in (True, False, None)
        assert cr._limit_on_tick(6.95, 123) is None
        assert cr._diff(cr._num(junk), 1.0) is None or isinstance(
            cr._diff(cr._num(junk), 1.0), float)

    def test_the_named_overflow_cases(self):
        assert cr._num(10 ** 400) is None
        assert cr._limit_on_tick(1e308, 'GOOGL') is None
        assert cr._diff(1e308, -1e308) is None        # inf is not a value

    @pytest.mark.parametrize('stamp', _JUNK_STAMPS, ids=repr)
    def test_junk_stamps(self, stamp):
        assert cr._iso(stamp) is None or isinstance(cr._iso(stamp), str)
        assert cr._age_s(stamp) is None or isinstance(cr._age_s(stamp), float)
        assert cr._same_tick(stamp, _stamp(1.0)) in (True, False, None)

    @pytest.mark.parametrize('junk', _JUNK, ids=repr)
    def test_placement_and_post_settle_and_leg_row(self, roller, junk):
        opportunity = {'underlying': junk, 'stock_bid': junk,
                       'stock_ask': junk, 'stock_quote_ts': junk,
                       'pricing_mode': junk, 'fc120': junk,
                       'old_option_symbol': junk}
        quote = {'bid': junk, 'ask': junk, 'timestamp': junk}
        for leg in ('btc', 'stc'):
            _all_primitive_and_finite(roller._placement_fields(
                opportunity, leg=leg, quote=quote, limit=junk, strike=junk,
                rung=junk, reused_from_rung=junk, chain_stamp=junk))
        roller.alpaca.get_option_quote.side_effect = lambda _s: dict(quote)
        roller.alpaca.get_stock_quote.side_effect = lambda _s: dict(quote)
        _all_primitive_and_finite(roller._post_settle_quote(
            'X', 'GOOGL', quote, 'sell'))
        with patch('src.strategy.call_roller.logger') as log:
            roller._log_leg_settled(
                symbol='X', underlying='GOOGL', order_id=junk,
                disposition='filled', placement={'leg': 'btc'}, limit=junk,
                requested_qty=junk, filled_qty=junk, filled_price=junk,
                order={'status': junk, 'submitted_at': junk}, elapsed=junk)
        for _e, kwargs in events(log):
            _all_primitive_and_finite(
                {k: v for k, v in kwargs.items() if k != 'timestamp_ms'})

    def test_a_swallowed_row_failure_leaves_one_breadcrumb(self, roller):
        with patch('src.strategy.call_roller.logger') as log:
            roller._log_leg_settled(
                symbol='X', underlying='GOOGL', order_id='o', disposition='filled',
                placement=None, limit=1.0, requested_qty=1)   # None.get raises
        assert event_types(log) == ['call_roll_instrumentation_failed']
        crumb = _first(log, 'call_roll_instrumentation_failed')
        assert crumb['where'] == 'leg_settled'
        assert crumb['event_category'] == 'system'
        assert log.error.call_count == 0      # low severity, not an error

    def test_bounded_read_reports_why(self):
        assert cr._bounded_read(lambda: {'bid': 1})[1] == 'ok'
        assert cr._bounded_read(lambda: {})[1] == 'empty'
        assert cr._bounded_read(lambda: Mock())[1] == 'empty'

        def boom():
            raise RuntimeError('x')
        assert cr._bounded_read(boom) == (None, 'error')
        gate = threading.Event()
        try:
            assert cr._bounded_read(lambda: gate.wait(30), timeout=0.05) == (
                None, 'timeout')
        finally:
            gate.set()


class _SampleAlpaca:
    """A read-only quote source for the sampler (it must never be asked to
    place, cancel or poll anything)."""

    def __init__(self, first, second, stock):
        self.reads = [first, second]
        self.stock = stock
        self.calls = []

    def get_option_quote(self, symbol):
        self.calls.append(('quote', symbol))
        return dict(self.reads.pop(0)) if self.reads else {}

    def get_stock_quote(self, symbol):
        self.calls.append(('stock', symbol))
        return dict(self.stock)


class TestTheEndOfCycleQuoteSampler:
    """Ruling D. One ``call_roll_quote_sample`` row per held short call; two
    back-to-back option reads plus the IEX quote; read-only; silent on
    failure; nothing once the cycle has run 600 s."""

    def test_one_row_with_the_feed_measurements(self):
        stamp = _stamp(3.0)
        alpaca = _SampleAlpaca(_q(7.90, 8.30, stamp=stamp),
                               _q(7.90, 8.32, stamp=stamp),
                               {'bid': 376.90, 'ask': 377.10,
                                'timestamp': _stamp(1.0)})
        with patch('src.strategy.call_roller.logger') as log:
            n = cr.sample_short_call_quotes(
                alpaca, [OLD_SYMBOL], {OLD_SYMBOL: 'not_itm_enough'},
                time.monotonic())
        assert n == 1
        row = _first(log, 'call_roll_quote_sample')
        assert row['strike'] == 370.0 and row['underlying'] == 'GOOGL'
        assert row['quote_ask'] == 8.30 and row['quote_reread_ask'] == 8.32
        assert row['same_tick'] is True
        assert row['quote_reread_delta_ask'] == 0.02
        assert row['quote_reread_delta_bid'] == 0.0
        assert row['quote_age_s'] == pytest.approx(3, abs=3)
        assert row['quote_feed'] == 'indicative'
        assert row['intrinsic'] == 6.90
        assert row['ask_minus_intrinsic'] == 1.40
        assert row['limit_on_tick'] is True       # the ask 8.30: nickel grid
        assert row['cycle_skip_reason'] == 'not_itm_enough'
        assert alpaca.calls == [('quote', OLD_SYMBOL), ('quote', OLD_SYMBOL),
                                ('stock', 'GOOGL')]
        _all_primitive_and_finite(
            {k: v for k, v in row.items() if k != 'timestamp_ms'})

    def test_failures_are_silent_and_rows_still_come(self):
        broken = Mock()
        broken.get_option_quote.side_effect = RuntimeError('down')
        broken.get_stock_quote.side_effect = RuntimeError('down')
        with patch('src.strategy.call_roller.logger') as log:
            n = cr.sample_short_call_quotes(broken, [OLD_SYMBOL, 'JUNK'], {},
                                            time.monotonic())
        assert n == 2
        assert all(r['quote_ask'] is None
                   for r in _all(log, 'call_roll_quote_sample'))
        assert log.error.call_count == 0

    def test_nothing_after_600_seconds(self):
        alpaca = _SampleAlpaca({}, {}, {})
        with patch('src.strategy.call_roller.logger') as log:
            n = cr.sample_short_call_quotes(alpaca, [OLD_SYMBOL], {},
                                            time.monotonic() - 600.0)
        assert n == 0 and alpaca.calls == []
        assert not _all(log, 'call_roll_quote_sample')
