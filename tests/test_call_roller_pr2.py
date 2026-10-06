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


# --------------------------------------------------------------------------- #
# T-11 — the poll split: BTC windows from the total; STO rungs their own
# --------------------------------------------------------------------------- #
class TestThePollSplit:
    """*Catches:* the split leaking into the ladder; the STO window defaulting
    to the BTC total; the settle losing its window or its two-read floor."""

    def _spy(self, roller, monkeypatch):
        calls = []
        real = roller._poll_order_fill

        def spy(order_id, timeout=None, *, min_reads=1):
            calls.append((order_id, timeout, min_reads))
            return real(order_id, 0, min_reads=min_reads)   # one read, no wait
        monkeypatch.setattr(roller, '_poll_order_fill', spy)
        monkeypatch.setattr(cr, '_CANCEL_SETTLE_TIMEOUT_SECONDS', 15)
        return calls

    @pytest.mark.parametrize("attempts, window", [(2, 40), (1, 60), (0, 120)])
    def test_the_btc_total_is_split_evenly(self, roller, instrumented,
                                           rolling_config, monkeypatch,
                                           attempts, window):
        rolling_config.rolling_btc_fill_timeout_seconds = 120
        rolling_config.rolling_btc_reprice_attempts = attempts
        rolling_config.rolling_stc_rung_timeout_seconds = 30
        calls = self._spy(roller, monkeypatch)
        script = {f'btc-{i}': _timeout(f'btc-{i}') for i in (1, 2, 3)}
        _scripted(roller, instrumented, script)

        primary = [t for oid, t, m in calls if m == 1]
        settles = [(t, m) for oid, t, m in calls if m != 1]
        assert primary == [window] * (attempts + 1)
        assert settles == [(15, 2)] * (attempts + 1)

    def test_every_sto_rung_polls_its_own_window(self, roller, instrumented,
                                                 rolling_config, monkeypatch):
        rolling_config.rolling_btc_fill_timeout_seconds = 120
        rolling_config.rolling_stc_rung_timeout_seconds = 30
        calls = self._spy(roller, monkeypatch)
        script = {'btc-1': {'poll': _o('btc-1', 'filled', 1, 8.35)},
                  'sto-1': _timeout('sto-1'), 'sto-2': _timeout('sto-2'),
                  'sto-3': _timeout('sto-3')}
        _scripted(roller, instrumented, script)

        by_order = {}
        for oid, t, m in calls:
            by_order.setdefault(oid, []).append((t, m))
        assert by_order['btc-1'] == [(40, 1)]
        for oid in ('sto-1', 'sto-2', 'sto-3'):
            assert by_order[oid] == [(30, 1), (15, 2)], oid


# --------------------------------------------------------------------------- #
# T-15 — the monotonic poll and the settle floor (FC-113 (b))
# --------------------------------------------------------------------------- #
class TestTheMonotonicPoll:
    """*Catches:* the RTT-blind loop FC-113 was filed about; the one-poll
    contract at ``timeout=0``; a settle that reads once and calls it verified."""

    @staticmethod
    def _rtt(mock_alpaca, clock, rtt, status='new'):
        def by_id(oid):
            clock.advance(rtt)
            return order(oid, status)
        mock_alpaca.get_order_by_id.side_effect = by_id

    def test_a_window_is_wall_clock_plus_one_trailing_read(
            self, roller, mock_alpaca, fake_clock):
        self._rtt(mock_alpaca, fake_clock, 3.0)
        start = fake_clock.now
        assert roller._poll_order_fill('o', timeout=30) is None
        elapsed = fake_clock.now - start
        assert elapsed <= 30 + 3.0, elapsed      # never 30 s of SLEEPS (48 s)
        assert elapsed >= 30
        assert all(s <= 5 for s in fake_clock.sleeps)
        assert fake_clock.sleeps[-1] == pytest.approx(3.0)   # min(5, remaining)

    def test_timeout_zero_is_one_read_and_no_sleep(self, roller, mock_alpaca,
                                                   fake_clock):
        self._rtt(mock_alpaca, fake_clock, 0.0)
        assert roller._poll_order_fill('o', timeout=0) is None
        assert roller._last_poll_reads == 1 and fake_clock.sleeps == []

    @pytest.mark.parametrize("rtt, reads, approx", [(2.0, 3, 16), (20.0, 2, 40)])
    def test_the_settle_floor_binds_on_slow_reads(self, roller, mock_alpaca,
                                                  fake_clock, rtt, reads,
                                                  approx):
        self._rtt(mock_alpaca, fake_clock, rtt)
        start = fake_clock.now
        assert roller._poll_order_fill('o', timeout=15, min_reads=2) is None
        assert roller._last_poll_reads == reads
        assert fake_clock.now - start == pytest.approx(approx)

    @pytest.mark.parametrize("rtt", [0.0, 2.0, 20.0])
    def test_cancel_and_settle_reads_at_least_twice(self, roller, mock_alpaca,
                                                    fake_clock, monkeypatch,
                                                    rtt):
        monkeypatch.setattr(cr, '_CANCEL_SETTLE_TIMEOUT_SECONDS', 15)
        self._rtt(mock_alpaca, fake_clock, rtt, status='pending_cancel')
        assert roller._cancel_and_settle('o') is None
        assert roller._last_poll_reads >= 2

    def test_a_terminal_read_still_returns_at_once(self, roller, mock_alpaca,
                                                   fake_clock):
        self._rtt(mock_alpaca, fake_clock, 1.0, status='filled')
        assert roller._poll_order_fill('o', timeout=30)['status'] == 'filled'
        assert roller._last_poll_reads == 1 and fake_clock.sleeps == []


# --------------------------------------------------------------------------- #
# T-16 — the success dict carries the pricing mode and both symbols
# --------------------------------------------------------------------------- #
class TestTheSuccessDict:
    """*Catches:* the DD-6 pricing-mode split and the sampler's rolled map
    losing their inputs."""

    def test_it_carries_them_and_the_replay_stamp_keeps_them(self, roller,
                                                             instrumented):
        script = {'btc-1': {'poll': _o('btc-1', 'filled', 1, 8.35)},
                  'sto-1': _timeout('sto-1'), 'sto-2': _timeout('sto-2'),
                  'sto-3': {'poll': _o('sto-3', 'filled', 1, 8.75)}}
        result, _log, _b = _scripted(roller, instrumented, script)
        assert result['success'] is True
        assert result['pricing_mode'] == 'base'
        assert result['old_option_symbol'] == _O
        # The ACTUAL replacement — here the C380 fallback, not the primary.
        assert result['new_option_symbol'] == _F

        from datetime import date
        from src.backtesting.engine.simulator import Simulator
        stamped = Simulator._stamp_roll_record(result, day=date(2026, 8, 4),
                                               close=377.0)
        for key in ('pricing_mode', 'old_option_symbol', 'new_option_symbol'):
            assert stamped[key] == result[key], key


# --------------------------------------------------------------------------- #
# T-17 — the STO ladder: the shipped shape (E = 0) and the escalation knob
# --------------------------------------------------------------------------- #
C385 = candidate(385.0, bid=8.60, ask=8.80, delta=0.30)
_G = C385['symbol']


def _ladder_run(roller, mock_alpaca, script, *, places=None, timeline=False):
    """Evaluate + execute against a scripted broker. ``places`` overrides the
    placement results in order (a dict refuses that placement)."""
    opp = roller.evaluate_roll_opportunity(call_position(), stock_position())
    assert opp is not None
    broker = _Broker(script)
    places = places or [accepted(oid) for oid in script]
    tl = _Timeline() if timeline else None
    if tl is not None:
        tl.wrap(mock_alpaca)
        mock_alpaca.place_option_order.side_effect = tl._recorder(
            'place_option_order', 'place', Mock(side_effect=places))
        mock_alpaca.get_order_by_id.side_effect = tl._recorder(
            'get_order_by_id', 'get_order', broker.by_id)
        mock_alpaca.cancel_order.side_effect = tl._recorder(
            'cancel_order', 'cancel', broker.cancel)
    else:
        mock_alpaca.place_option_order.side_effect = places
        mock_alpaca.get_order_by_id.side_effect = broker.by_id
        mock_alpaca.cancel_order.side_effect = broker.cancel
    with patch('src.strategy.call_roller.logger') as log:
        if tl is not None:
            tl.log_sink(log)
        result = roller.execute_roll(opp)
    return result, log, tl


def _stos(log):
    return [(p['rung'], p['rung_kind'], p['limit_price'])
            for p in _all(log, 'call_roll_stc_placed')]


BTC_FILL_835 = {'poll': _o('btc-1', 'filled', 1, 8.35)}


class TestStcLadder:
    """*Mutations:* escalate before the settle → (a'); escalate after a
    primary-poll terminal → (l); price from the fresh bid alone → (b); place a
    floor after a rung-1-at-floor → T-8; drop ``max(..., floor)`` → T-8."""

    def test_a_shipped_e0_the_floor_follows_with_no_read_in_between(
            self, roller, instrumented):
        script = {'btc-1': BTC_FILL_835, 'sto-1': _timeout('sto-1'),
                  'sto-2': {'poll': _o('sto-2', 'filled', 1, 10.90)}}
        result, log, tl = _ladder_run(roller, instrumented, script,
                                      timeline=True)
        assert result['success'] is True
        assert _stos(log) == [(1, 'primary', 10.80), (2, 'floor', 8.35)]
        path = tl.order_path()
        settle = path.index(('get_order', 'sto-1'), path.index(('cancel', 'sto-1')))
        floor = path.index(('place', (_N, 'sell', 8.35)))
        assert path[settle + 1:floor] == [], path[settle + 1:floor]

    def test_a2_e2_one_basis_read_after_our_settle_then_rung_2(
            self, roller, instrumented, rolling_config):
        rolling_config.rolling_stc_escalation_rungs = 2
        instrumented.get_option_quote.side_effect = _book(**{
            _N: {'pricing': [_q(10.90, 11.10), _q(10.90, 11.10), _q(10.70, 10.90)],
                 'diag': [_q(10.92, 11.12)]}})
        script = {'btc-1': BTC_FILL_835, 'sto-1': _timeout('sto-1'),
                  'sto-2': {'poll': _o('sto-2', 'filled', 1, 10.80)}}
        result, log, tl = _ladder_run(roller, instrumented, script,
                                      timeline=True)
        assert _stos(log) == [(1, 'primary', 10.80), (2, 'escalation', 10.50)]
        path = tl.order_path()
        settle = path.index(('get_order', 'sto-1'), path.index(('cancel', 'sto-1')))
        assert path[settle + 1:settle + 3] == [
            ('pricing_quote', _N), ('place', (_N, 'sell', 10.5))]
        rung2 = _all(log, 'call_roll_stc_placed')[1]
        assert rung2['escalation_index'] == 1
        assert rung2['basis_bid'] == 10.70 and rung2['stc_quote_source'] == 'fresh'
        assert rung2['prior_rung_disposition'] == 'timeout_canceled'
        # The basis read doubled as rung 1's cancel quote: one read, two uses.
        rung1_row = [r for r in _all(log, 'call_roll_leg_settled')
                     if r.get('rung') == 1][0]
        assert rung1_row['cancel_quote_bid'] == 10.70

    @pytest.mark.parametrize("fresh, source, limit", [
        (_q(11.20, 11.40), 'fresh', 10.70),     # (b) min keeps the basis
        ({}, 'prior_basis', 10.70),              # (c) the read failed
    ])
    def test_b_c_the_escalation_basis(self, roller, instrumented,
                                      rolling_config, fresh, source, limit):
        rolling_config.rolling_stc_escalation_rungs = 2
        instrumented.get_option_quote.side_effect = _book(**{
            _N: {'pricing': [_q(10.90, 11.10), _q(10.90, 11.10), fresh],
                 'diag': [_q(10.92, 11.12)]}})
        script = {'btc-1': BTC_FILL_835, 'sto-1': _timeout('sto-1'),
                  'sto-2': {'poll': _o('sto-2', 'filled', 1, 10.80)}}
        _r, log, _t = _ladder_run(roller, instrumented, script)
        rung2 = _all(log, 'call_roll_stc_placed')[1]
        assert rung2['limit_price'] == limit
        assert rung2['stc_quote_source'] == source and rung2['basis_bid'] == 10.90

    def test_d_an_escalation_at_or_below_the_floor_is_the_floor(
            self, roller, instrumented, rolling_config):
        rolling_config.rolling_stc_escalation_rungs = 2
        instrumented.get_option_quote.side_effect = _book(**{
            _N: {'pricing': [_q(10.90, 11.10), _q(10.90, 11.10), _q(10.65, 10.85)],
                 'diag': [_q(10.92, 11.12)]}})
        script = {'btc-1': {'poll': _o('btc-1', 'filled', 1, 10.50)},
                  'sto-1': _timeout('sto-1'),
                  'sto-2': {'poll': _o('sto-2', 'filled', 1, 10.60)}}
        _r, log, _t = _ladder_run(roller, instrumented, script)
        # 10.65 - 0.20 = 10.45 <= floor 10.50: the floor, in rung 2's slot.
        assert _stos(log) == [(1, 'primary', 10.80), (2, 'floor', 10.50)]

    def test_f_a_synchronous_rejection_escalates_from_a_fresh_read(
            self, roller, instrumented, rolling_config):
        rolling_config.rolling_stc_escalation_rungs = 2
        instrumented.get_option_quote.side_effect = _book(**{
            _N: {'pricing': [_q(10.90, 11.10), _q(10.90, 11.10), _q(10.80, 11.00)],
                 'diag': [_q(10.92, 11.12)]}})
        script = {'btc-1': BTC_FILL_835,
                  'sto-2': {'poll': _o('sto-2', 'filled', 1, 10.70)}}
        places = [accepted('btc-1'), {'success': False, 'error_message': 'no'},
                  accepted('sto-2')]
        result, log, _t = _ladder_run(roller, instrumented, script,
                                      places=places)
        assert result['success'] is True
        assert _stos(log) == [(1, 'primary', 10.80), (2, 'escalation', 10.60)]
        filled = _first(log, 'call_roll_stc_filled')
        assert filled['prior_rung_disposition'] == 'rejected'
        assert filled['prior_rung_miss_offset'] is None    # a refusal, not a miss
        assert filled['primary_miss_offset'] is None

    def test_g_an_unknown_settle_stops_the_ladder_with_no_basis_read(
            self, roller, instrumented, rolling_config):
        rolling_config.rolling_stc_escalation_rungs = 2
        script = {'btc-1': BTC_FILL_835,
                  'sto-1': _timeout('sto-1', after='pending_cancel')}
        result, log, _t = _ladder_run(roller, instrumented, script)
        assert result['reason'] == 'stc_disposition_unknown'
        assert len(_all(log, 'call_roll_stc_placed')) == 1
        counts = instrumented.get_option_quote.side_effect.counts
        assert counts[(_N, 'pricing')] == 2   # execute-time + rung-1 fresh only

    @pytest.fixture
    def three_candidates(self, instrumented, mock_market_data):
        """C375 primary; C380 (+$20) and C385 (+$0) as the two fallbacks."""
        mock_market_data.find_suitable_calls.return_value = [
            dict(C380), dict(C375), dict(C385)]
        instrumented.get_option_quote.side_effect = _book(**{
            _G: [_q(8.60, 8.80), _q(8.62, 8.82)]})
        return instrumented

    @pytest.mark.parametrize("escalations, expected", [
        (2, [(1, 'primary', 0), (2, 'escalation', 1), (3, 'escalation', 2),
             (4, 'floor', None), (5, 'fallback', None), (6, 'fallback', None)]),
        (0, [(1, 'primary', 0), (2, 'floor', None), (3, 'fallback', None),
             (4, 'fallback', None)]),
    ])
    def test_i_j_every_rung_by_position_and_the_floors_reused_quote(
            self, roller, three_candidates, rolling_config, escalations,
            expected):
        rolling_config.rolling_stc_escalation_rungs = escalations
        script = {'btc-1': BTC_FILL_835}
        script.update({f'sto-{i}': _timeout(f'sto-{i}') for i in range(1, 7)})
        result, log, _t = _ladder_run(roller, three_candidates, script)
        assert result['reason'] == 'stc_failed_naked_exposure'
        placed = _all(log, 'call_roll_stc_placed')
        assert [(p['rung'], p['rung_kind'], p['escalation_index'])
                for p in placed] == expected
        floor = [p for p in placed if p['rung_kind'] == 'floor'][0]
        assert floor['quote_reused_from_rung'] == (3 if escalations else 1)
        assert floor['limit_price'] == 8.35
        assert [p['primary_limit'] for p in placed] == [10.80] * len(placed)
        assert all(p['limit_price'] >= 8.35 for p in placed)
        rows = [r for r in _all(log, 'call_roll_leg_settled') if r['leg'] == 'stc']
        assert len(rows) == len(placed)

    def test_k_rung_1_is_the_lower_of_two_reads_aged_by_the_fresh_stamp(
            self, roller, instrumented):
        instrumented.get_option_quote.side_effect = _book(**{
            _N: {'pricing': [_q(10.90, 11.10), _q(10.86, 11.06, age_s=2.0)],
                 'diag': [_q(10.92, 11.12)]}})
        script = {'btc-1': BTC_FILL_835,
                  'sto-1': {'poll': _o('sto-1', 'filled', 1, 10.86)}}
        _r, log, _t = _ladder_run(roller, instrumented, script)
        rung1 = _first(log, 'call_roll_stc_placed')
        assert rung1['basis_bid'] == 10.86 and rung1['pre_btc_bid'] == 10.90
        assert rung1['limit_price'] == 10.75          # snap_down(10.76)
        assert rung1['quote_age_s'] == pytest.approx(2, abs=2)

    @pytest.mark.parametrize("status", ['expired', 'canceled'])
    def test_l_a_primary_poll_terminal_goes_straight_to_the_floor(
            self, roller, instrumented, rolling_config, status):
        rolling_config.rolling_stc_escalation_rungs = 2
        script = {'btc-1': BTC_FILL_835, 'sto-1': {'poll': _o('sto-1', status)},
                  'sto-2': {'poll': _o('sto-2', 'filled', 1, 10.90)}}
        _r, log, _t = _ladder_run(roller, instrumented, script)
        assert _stos(log) == [(1, 'primary', 10.80), (2, 'floor', 8.35)]
        assert 'call_roll_stc_unfilled' in event_types(log)
        counts = instrumented.get_option_quote.side_effect.counts
        assert counts[(_N, 'pricing')] == 2   # no escalation basis read

    def test_m_imminence_pads_rung_1_and_escalates_in_base_mode(
            self, roller, instrumented, rolling_config):
        rolling_config.rolling_stc_escalation_rungs = 2
        instrumented.get_option_quote.side_effect = _book(old=(7.00, 7.20))
        script = {'btc-1': {'poll': _o('btc-1', 'filled', 1, 7.15)},
                  'sto-1': _timeout('sto-1'),
                  'sto-2': {'poll': _o('sto-2', 'filled', 1, 10.90)}}
        _r, log, _t = _ladder_run(roller, instrumented, script)
        placed = _all(log, 'call_roll_stc_placed')
        assert [(p['limit_price'], p['limit_formula']) for p in placed] == [
            (10.95, 'imminence'), (10.70, 'base')]

    @pytest.mark.parametrize("fresh_bid, kinds", [
        # 10.65 - 0.20 = 10.45 <= floor 10.50: rung 2 IS the floor; refused,
        # the ladder goes to the fallback — never a second floor.
        (10.65, [(1, 'primary'), (2, 'floor'), (3, 'fallback')]),
        # 10.90 - 0.20 = 10.70 > floor: an escalation; refused, the floor next.
        (10.90, [(1, 'primary'), (2, 'escalation'), (3, 'floor')]),
    ])
    def test_n_the_floor_guard_against_a_refused_rung(
            self, roller, instrumented, rolling_config, fresh_bid, kinds):
        rolling_config.rolling_stc_escalation_rungs = 1
        # C380 rich enough (10.70 buffered) to clear the 10.50 fill as a
        # fallback, still behind C375 on credit.
        instrumented.get_option_quote.side_effect = _book(c380=(10.80, 11.00), **{
            _N: {'pricing': [_q(10.90, 11.10), _q(10.90, 11.10),
                             _q(fresh_bid, fresh_bid + 0.20)],
                 'diag': [_q(10.92, 11.12)]}})
        script = {'btc-1': {'poll': _o('btc-1', 'filled', 1, 10.50)},
                  'sto-1': _timeout('sto-1'),
                  'sto-3': {'poll': _o('sto-3', 'filled', 1, 10.90)}}
        places = [accepted('btc-1'), accepted('sto-1'),
                  {'success': False, 'error_message': 'price protection'},
                  accepted('sto-3')]
        _r, log, _t = _ladder_run(roller, instrumented, script, places=places)
        assert [(r, k) for r, k, _l in _stos(log)] == kinds
        assert sum(1 for _r2, k, _l in _stos(log) if k == 'floor') == 1

