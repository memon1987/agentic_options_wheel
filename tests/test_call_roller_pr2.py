"""FC-120 PR-2 — the roller's policy change, test by test (docs/plans/fc-120.md).

T-7 / T-8 (pricing and the invariant at every site), T-10 (the BTC re-price
state machine), T-11 (the poll split), T-15 (the monotonic poll and the settle
floor), T-16 (the success dict), T-17 (the STO ladder and the escalation
knob), T-19 (the budget, MEASURED), T-20 (the quiet data plane through
``_bounded_read``), T-21 (deferred rows survive an escaped exception) and T-22
(the sampler on a rolled call). The PR-1 suite and its re-pinned goldens (T-18)
stay in ``tests/test_call_roller.py``, whose fixtures and builders this module
imports by NAME (never ``*`` — that would collect its test classes twice).

Every test names the regression it catches.
"""

import random
import threading
import time
import types
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest

from src.strategy import call_roller as cr
from src.strategy import roll_budget
from src.strategy.call_roller import CallRoller
from tests.test_call_roller import (  # noqa: F401 - fixtures are used by name
    C375, C380, FakeClock, OLD_SYMBOL, TERMINAL_EVENTS, _Broker, _Timeline,
    _all, _book, _drive, _drive_timeline, _first, _no_settle_sleep, _o, _q,
    _sequenced, accepted, call_position, candidate, event_types, events,
    fake_clock, instrumented, mock_alpaca, mock_earnings, mock_market_data,
    mock_risk_manager, order, roller, rolling_config, stock_position, terminals)

_O, _N, _F = OLD_SYMBOL, C375['symbol'], C380['symbol']


def _limits(mock_alpaca, side=None):
    return [c.kwargs['limit_price']
            for c in mock_alpaca.place_option_order.call_args_list
            if side is None or c.kwargs['side'] == side]


# --------------------------------------------------------------------------- #
# T-7 — base limits buffered, parity-floored, on tick; imminence padded only
# --------------------------------------------------------------------------- #
class TestThePricingRule:
    """*Catches:* the buffer stacking on the imminence pad; the parity floor
    applied as a pad; a limit off the tick grid; a buy rounded down."""

    @staticmethod
    def btc(ask, *, mid=None, stock_bid=350.0, strike=345.0, n=1, buffer=0.10,
            formula='base', underlying='GOOGL'):
        return CallRoller._btc_limit_from_quote(
            ask, formula=formula, mid=mid, stock_bid=stock_bid, strike=strike,
            buffer_multiple=n, buffer=buffer, underlying=underlying)

    @staticmethod
    def stc(bid, ask=None, *, n=1, buffer=0.10, floor=None, formula='base',
            underlying='GOOGL'):
        return CallRoller._stc_limit_from_quote(
            bid, ask, formula, buffer_multiple=n, buffer=buffer, floor=floor,
            underlying=underlying)

    def test_the_buy_to_close(self):
        assert self.btc(8.40) == 8.50                    # ask + buffer
        assert self.btc(8.41) == 8.55                    # snapped UP
        # Parity 353.60 - 345 + 0.01 = 8.61 > 8.50: the FLOOR binds.
        assert self.btc(8.40, stock_bid=353.60) == 8.65
        assert cr._parity_applied(8.40, formula='base', mid=None,
                                  stock_bid=353.60, strike=345.0,
                                  buffer_multiple=1, buffer=0.10) is True
        assert cr._parity_applied(8.40, formula='base', mid=None,
                                  stock_bid=350.0, strike=345.0,
                                  buffer_multiple=1, buffer=0.10) is False

    def test_the_sell_to_open(self):
        assert self.stc(10.90) == 10.80
        assert self.stc(7.43, underlying='NVDA') == 7.30  # snapped DOWN past 0.10
        assert self.stc(2.982, underlying='IWM') == 2.88  # always-penny
        assert self.stc(0.08) is None                     # under the buffer

    def test_imminence_pads_the_first_placement_and_never_stacks_the_buffer(self):
        assert self.btc(7.20, mid=7.15, formula='imminence') == 7.20
        assert self.stc(7.05, 7.25, formula='imminence') == 7.10
        # The parity floor DOES apply in imminence — a marketability floor.
        assert self.btc(7.20, mid=7.15, stock_bid=353.0,
                        formula='imminence') == 8.05

    def test_a_zero_buffer_leaves_parity_and_the_snap(self):
        assert self.btc(8.41, buffer=0.0) == 8.45        # snap_up(ask)
        assert self.btc(8.41, buffer=0.0, stock_bid=354.0) == 9.05  # parity
        assert self.stc(10.92, buffer=0.0) == 10.90

    def test_no_stock_bid_omits_the_floor_and_never_raises(self):
        assert self.btc(8.40, stock_bid=None) == 8.50
        assert cr._parity_stock_bid(None, 'execution') == (None, 'none')
        assert cr._parity_stock_bid({'bid': 287.82, 'ask': 318.75},
                                    'execution') == (None, 'none')
        assert cr._parity_stock_bid({'bid': 350.0, 'ask': 350.2},
                                    'reprice') == (350.0, 'reprice')

    @pytest.mark.parametrize("ask", [3.00, 2.98, 5.37, 11.71, 0.43])
    def test_every_limit_is_on_tick(self, ask):
        for underlying in ('GOOGL', 'IWM'):
            for n in (1, 2, 3):
                buy = self.btc(ask, n=n, underlying=underlying)
                tick = cr.tick_size(cr._dec(buy), underlying)
                assert cr._dec(buy) % tick == 0, (ask, n, underlying, buy)
                assert buy >= ask + 0.10 * n - 1e-9, "a buy rounded DOWN"

    def test_a_failing_execute_time_stock_read_omits_parity_without_raising(
            self, roller, mock_alpaca):
        opp = roller.evaluate_roll_opportunity(call_position(), stock_position())
        mock_alpaca.get_stock_quote.side_effect = RuntimeError("iex down")
        mock_alpaca.place_option_order.side_effect = [
            accepted('btc-1'), accepted('sto-1')]
        mock_alpaca.get_order_by_id.side_effect = lambda oid: order(
            oid, 'filled', 1, 8.40 if oid == 'btc-1' else 10.90)
        with patch('src.strategy.call_roller.logger') as log:
            result = roller.execute_roll(opp)
        assert result['success'] is True
        placed = _first(log, 'call_roll_btc_placed')
        assert placed['stock_quote_source'] == 'none'
        assert placed['parity_floor_applied'] is False
        assert placed['parity_stock_bid'] is None


# --------------------------------------------------------------------------- #
# T-8 — the invariant on the BUFFERED, snapped limits at every site
# --------------------------------------------------------------------------- #
def _scripted(roller, mock_alpaca, script, opp=None):
    """Arm a scripted broker and run one ``execute_roll``."""
    opp = opp or roller.evaluate_roll_opportunity(call_position(), stock_position())
    broker = _Broker(script)
    mock_alpaca.place_option_order.side_effect = [
        accepted(oid) for oid in list(script) + [f'x-{i}' for i in range(6)]]
    mock_alpaca.get_order_by_id.side_effect = broker.by_id
    mock_alpaca.cancel_order.side_effect = broker.cancel
    with patch('src.strategy.call_roller.logger') as log:
        result = roller.execute_roll(opp)
    return result, log, broker


class TestTheInvariantAtEverySite:
    """*Catches:* any site still pricing off the raw quote; a rung below the
    invariant; a floor placed twice."""

    def test_the_screen_refuses_a_raw_flat_roll(self, roller, mock_market_data,
                                                mock_alpaca, rolling_config):
        """Raw bid - ask is exactly the 0.20/contract minimum; the buffered
        pair (8.50 / 8.50) nets 0 < 0.002/share — refused AT THE SCREEN."""
        rolling_config.rolling_min_net_credit_per_contract = 0.20
        thin = candidate(375.0, bid=8.42, ask=8.60)
        mock_market_data.find_suitable_calls.return_value = [thin]
        mock_alpaca.get_option_quote.side_effect = lambda s: dict(
            {OLD_SYMBOL: {'bid': 8.00, 'ask': 8.40}, thin['symbol']: thin}.get(s, {}))
        with patch('src.strategy.call_roller.logger') as log:
            assert roller.evaluate_roll_opportunity(
                call_position(), stock_position()) is None
        skip = _first(log, 'call_roll_skipped')
        assert skip['skip_reason'] == 'no_credit_candidate'

    @pytest.mark.parametrize("underlying, fill, floor", [
        ('GOOGL', 8.35, 8.40),    # nickel grid: fill + one tick
        ('IWM', 1.93, 1.94),      # always-penny: fill + one tick
        ('GOOGL', 2.97, 2.98),    # penny below $3.00
    ])
    def test_the_floor_is_the_fill_plus_one_tick_at_twenty_cents(
            self, roller, underlying, fill, floor):
        opp = {'underlying': underlying, 'min_credit_per_share': 0.20 / 100,
               'imminent': False}
        assert roller._new_ladder(opp, fill)['floor'] == pytest.approx(floor)

    def test_rung_1_at_the_floor_is_the_floor_and_no_second_one_follows(
            self, roller, instrumented, rolling_config):
        """R6-R: the fresh rung-1 bid has fallen below fill + min + buffer, so
        rung 1 is PLACED at the floor — logged rung 1, kind floor, no reused
        quote — and the next rung is a fallback, never a second floor."""
        rolling_config.rolling_stc_escalation_rungs = 2
        instrumented.get_option_quote.side_effect = _book(**{
            _N: {'pricing': [_q(10.90, 11.10), _q(8.40, 8.60)],
                 'diag': [_q(8.40, 8.60)]}})
        script = {'btc-1': {'poll': _o('btc-1', 'filled', 1, 8.30)},
                  'sto-1': {'poll': _o('sto-1', 'new'),
                            'after_cancel': _o('sto-1', 'canceled')},
                  'sto-2': {'poll': _o('sto-2', 'filled', 1, 8.80)}}
        result, log, _b = _scripted(roller, instrumented, script)

        assert result['success'] is True
        placed = _all(log, 'call_roll_stc_placed')
        # No escalation was placed, so the floor's slot is 2 (unused) and the
        # first fallback takes position 3 — numbering never reuses a slot.
        assert [(p['rung'], p['rung_kind'], p['limit_price']) for p in placed] == [
            (1, 'floor', 8.30), (3, 'fallback', 8.70)]
        assert placed[0]['quote_reused_from_rung'] is None
        assert 'call_roll_btc_repriced' not in event_types(log)

    def test_every_escalation_rung_sits_at_or_above_the_floor(self, roller):
        """Property-style: 200 random bases and fills, E = 3 — no rung the
        escalation helper returns is ever at or below the floor (that rung IS
        the floor and the helper says so)."""
        rng = random.Random(120)
        for _ in range(200):
            fill = round(rng.uniform(0.50, 20.0), 2)
            floor = roller._new_ladder(
                {'underlying': 'GOOGL', 'min_credit_per_share': 0.002,
                 'imminent': False}, fill)['floor']
            basis = round(rng.uniform(0.20, 25.0), 2)
            fresh = rng.choice([None, round(rng.uniform(0.20, 25.0), 2)])
            for n in (1, 2, 3):
                limit, rq = roller._escalation_rung(
                    basis, n, floor, fresh, buffer=0.10, underlying='GOOGL')
                if limit is not None:
                    assert limit > floor, (basis, fresh, n, floor, limit)
                    assert rq['rung_kind'] == 'escalation'

    def test_a_fallback_is_tested_with_its_buffered_limit_against_the_fill(
            self, roller, instrumented):
        """C380 at 8.44 bid -> 8.34 buffered: below the 8.35 fill (and the
        floor) by a cent. Refused — the raw 8.44 would have cleared."""
        instrumented.get_option_quote.side_effect = _book(
            c380=(8.80, 9.00), **{_F: [_q(8.44, 8.64)]})
        script = {'btc-1': {'poll': _o('btc-1', 'filled', 1, 8.35)},
                  'sto-1': {'poll': _o('sto-1', 'expired')},
                  'sto-2': {'poll': _o('sto-2', 'expired')}}
        result, log, _b = _scripted(roller, instrumented, script)

        sold = [c.kwargs['symbol'] for c in
                instrumented.place_option_order.call_args_list
                if c.kwargs['side'] == 'sell']
        assert _F not in sold
        assert result['reason'] == 'stc_failed_naked_exposure'


# --------------------------------------------------------------------------- #
# T-10 — the BTC re-price state machine (scripted broker: `canceled` arrives
# only after the roller's OWN cancel; a `canceled` from the primary poll is
# "someone else canceled it")
# --------------------------------------------------------------------------- #
def _timeout(oid, after='canceled', **kw):
    return {'poll': _o(oid, 'new'), 'after_cancel': _o(oid, after, **kw)}


class TestBtcReprice:
    """*Mutations:* drop the settle before the re-price → (b); re-price over
    an unknown settle → (c); re-price a ``rejected`` → (h); re-price after a
    primary-poll terminal → (g); keep the pad on a re-price → (l)."""

    def test_a_reprice_after_our_own_zero_fill_cancel(self, roller,
                                                      instrumented):
        """(a)(i)(k)(m)(n): attempt 0 times out and settles canceled; the fresh
        ask is higher, so attempt 1 is placed at snap_up(ask + 2 x buffer),
        fills, and the ladder runs off attempt 1's fill. Attempt 0's settled
        row carries the re-price read as its cancel quote — no second read."""
        instrumented.get_option_quote.side_effect = _book(**{
            _O: {'pricing': [_q(8.00, 8.40), _q(8.00, 8.40), _q(8.10, 8.50)],
                 'diag': [_q(8.02, 8.42)]},
            _N: {'pricing': [_q(10.90, 11.10), _q(10.70, 10.90), _q(10.95, 11.15)],
                 'diag': [_q(10.92, 11.12)]}})
        script = {'btc-1': _timeout('btc-1'),
                  'btc-2': {'poll': _o('btc-2', 'filled', 1, 8.55)},
                  'sto-1': {'poll': _o('sto-1', 'filled', 1, 10.75)}}
        result, log, _b = _scripted(roller, instrumented, script)

        assert result['success'] is True
        assert _limits(instrumented) == [8.50, 8.70, 10.60]
        rep = _first(log, 'call_roll_btc_repriced')
        assert rep['attempt'] == 1 and rep['prior_order_id'] == 'btc-1'
        assert (rep['prior_limit'], rep['new_limit']) == (8.50, 8.70)
        assert (rep['prior_quote_ask'], rep['quote_ask']) == (8.40, 8.50)
        assert rep['limit_formula'] == 'base'
        assert rep['btc_quote_source'] == 'reprice'
        assert rep['stc_limit'] == 10.60
        # The ladder ran off attempt 1's FILL (8.55): floor = 8.55 + $0.
        completed = [k for e, k in events(log) if e == 'call_roll_completed']
        assert completed and completed[0]['btc_filled_price'] == 8.55
        # (m) rung 1's pre-BTC basis is the RE-PRICE read (10.70), not the
        # execute-time one (10.90): min(10.70, fresh 10.95) - 0.10 = 10.60.
        rung1 = _first(log, 'call_roll_stc_placed')
        assert rung1['pre_btc_bid'] == 10.70
        assert rung1['pre_btc_quote_source'] == 'reprice'
        assert rung1['basis_bid'] == 10.70
        # (k) attempt 0's row: the re-price read is its cancel-time quote.
        rows = [r for r in _all(log, 'call_roll_leg_settled') if r['leg'] == 'btc']
        assert [(r['attempt'], r['disposition']) for r in rows] == [
            (0, 'timeout_canceled'), (1, 'filled')]
        assert rows[0]['cancel_quote_ask'] == 8.50
        assert instrumented.get_option_quote.side_effect.counts[(_O, 'diag')] == 2
        # (n) provenance on every placed BTC row.
        placed = _all(log, 'call_roll_btc_placed')
        assert [p['attempt'] for p in placed] == [0, 1]
        assert all(p['stock_quote_source'] in ('execution', 'reprice')
                   and p['parity_floor_applied'] is False for p in placed)

    def test_b_a_cancel_that_lost_the_race_is_a_fill_not_a_reprice(
            self, roller, instrumented):
        script = {'btc-1': {'poll': _o('btc-1', 'new'),
                            'after_cancel': _o('btc-1', 'filled', 1, 8.40)},
                  'sto-1': {'poll': _o('sto-1', 'filled', 1, 10.90)}}
        result, log, _b = _scripted(roller, instrumented, script)
        assert result['success'] is True
        assert 'call_roll_btc_repriced' not in event_types(log)
        assert _limits(instrumented, 'buy') == [8.50]

    def test_c_an_unknown_settle_never_places_a_second_order(self, roller,
                                                             instrumented):
        script = {'btc-1': _timeout('btc-1', after='pending_cancel')}
        result, log, _b = _scripted(roller, instrumented, script)
        assert result['reason'] == 'btc_disposition_unknown'
        assert instrumented.place_option_order.call_count == 1
        assert terminals(log) == ['call_roll_unknown_disposition']

    def test_d_a_credit_gone_at_reprice_is_one_timeout_terminal(
            self, roller, instrumented):
        instrumented.get_option_quote.side_effect = _book(**{
            _N: {'pricing': [_q(10.90, 11.10), _q(8.50, 8.70)],
                 'diag': [_q(8.50, 8.70)]}})
        result, log, _b = _scripted(roller, instrumented,
                                    {'btc-1': _timeout('btc-1')})
        assert result['reason'] == 'btc_timeout_canceled'
        assert terminals(log) == ['call_roll_btc_timeout_canceled']
        ev = _first(log, 'call_roll_btc_timeout_canceled')
        assert ev['reprice_skipped_reason'] == 'credit_gone'
        assert (ev['btc_limit_fresh'], ev['stc_limit_fresh']) == (8.60, 8.40)
        assert ev['attempts'] == 1
        assert 'call_roll_skipped' not in event_types(log)
        assert instrumented.place_option_order.call_count == 1

    def test_f_exhausted_attempts_step_the_buffer_and_end_in_one_terminal(
            self, roller, instrumented):
        script = {f'btc-{i}': _timeout(f'btc-{i}') for i in (1, 2, 3)}
        result, log, _b = _scripted(roller, instrumented, script)
        assert _limits(instrumented) == [8.50, 8.60, 8.70]   # (i): (n+1) x 0.10
        ev = _first(log, 'call_roll_btc_timeout_canceled')
        assert ev['attempts'] == 3 and ev['disposition'] == 'timeout_canceled'
        assert terminals(log) == ['call_roll_btc_timeout_canceled']
        assert len(_all(log, 'call_roll_leg_settled')) == 3
        assert event_types(log).count('call_roll_btc_repriced') == 2

    @pytest.mark.parametrize("status", ['expired', 'canceled'])
    def test_g_a_primary_poll_terminal_is_terminal(self, roller, instrumented,
                                                   status):
        """(g)/(g'): `expired` at the DAY close, or `canceled` by ANOTHER actor
        — the roller did not cancel it, so it does not re-price (R6-B)."""
        script = {'btc-1': {'poll': _o('btc-1', status)}}
        result, log, _b = _scripted(roller, instrumented, script)
        assert result['reason'] == 'btc_timeout_canceled'
        ev = _first(log, 'call_roll_btc_timeout_canceled')
        assert ev['disposition'] == 'terminal_no_fill' and ev['attempts'] == 1
        instrumented.cancel_order.assert_not_called()
        assert 'call_roll_btc_repriced' not in event_types(log)

    def test_h_a_rejected_after_placement_is_never_repriced(self, roller,
                                                            instrumented):
        script = {'btc-1': {'poll': _o('btc-1', 'rejected')}}
        result, log, _b = _scripted(roller, instrumented, script)
        assert result['reason'] == 'btc_rejected'
        rows = _all(log, 'call_roll_leg_settled')
        assert [r['disposition'] for r in rows] == ['rejected']
        assert instrumented.place_option_order.call_count == 1

    def test_j_a_failed_reprice_read_prices_from_the_prior_basis(
            self, roller, instrumented):
        instrumented.get_option_quote.side_effect = _book(**{
            _O: {'pricing': [_q(8.00, 8.40), _q(8.00, 8.40), {}],
                 'diag': [_q(8.02, 8.42)]}})
        script = {'btc-1': _timeout('btc-1'),
                  'btc-2': {'poll': _o('btc-2', 'filled', 1, 8.40)},
                  'sto-1': {'poll': _o('sto-1', 'filled', 1, 10.90)}}
        result, log, _b = _scripted(roller, instrumented, script)
        assert _limits(instrumented, 'buy') == [8.50, 8.60]
        rep = _first(log, 'call_roll_btc_repriced')
        assert rep['btc_quote_source'] == 'prior_basis'
        # The failed read is still attempt 0's cancel quote: absent, no retry.
        row0 = [r for r in _all(log, 'call_roll_leg_settled')
                if r['leg'] == 'btc' and r['attempt'] == 0][0]
        assert row0['cancel_quote_ask'] is None

    def test_j2_both_bases_unusable_refuses_with_quote_unusable(
            self, roller, instrumented, monkeypatch):
        """Unreachable without injection — the prior basis priced attempt 0 —
        so `_price_btc` is made to find nothing at the re-price."""
        real = roller._price_btc
        calls = {'n': 0}

        def price(*a, **k):
            calls['n'] += 1
            return None if calls['n'] > 1 else real(*a, **k)
        monkeypatch.setattr(roller, '_price_btc', price)
        result, log, _b = _scripted(roller, instrumented,
                                    {'btc-1': _timeout('btc-1')})
        ev = _first(log, 'call_roll_btc_timeout_canceled')
        assert ev['reprice_skipped_reason'] == 'quote_unusable'
        assert terminals(log) == ['call_roll_btc_timeout_canceled']

    def test_l_imminence_pads_attempt_0_and_reprices_in_base_mode(
            self, roller, instrumented):
        """R6-C: mid 7.10 + 0.05 = 7.15 (pad, no buffer); the re-price is the
        BASE formula off the fresh ask — 7.20 + 2 x 0.10 = 7.40 — while
        `pricing_mode` still names imminence on every row."""
        instrumented.get_option_quote.side_effect = _book(old=(7.00, 7.20))
        script = {'btc-1': _timeout('btc-1'),
                  'btc-2': {'poll': _o('btc-2', 'filled', 1, 7.20)},
                  'sto-1': {'poll': _o('sto-1', 'filled', 1, 10.95)}}
        result, log, _b = _scripted(roller, instrumented, script)
        placed = _all(log, 'call_roll_btc_placed')
        assert [(p['limit_price'], p['limit_formula'], p['pricing_mode'])
                for p in placed] == [(7.15, 'imminence', 'imminence'),
                                     (7.40, 'base', 'imminence')]
        assert result['success'] is True

