"""`find_suitable_calls` — the FC-013 earnings SPAN criterion.

Plan: docs/plans/fc-013.md DD-3 / §3.

The predicate under test is `expiration_date >= next_earnings_date`, applied
per candidate, last in the criteria chain. Three properties matter and each has
a test that fails if it is lost:

1. A candidate that expires INTO the event is rejected and counted.
2. A candidate that expires BEFORE the event is allowed — at any DTE. This is
   what makes span DTE-invariant, and it is the property a hardcoded N or a
   derive-N-from-DTE implementation destroys.
3. `>=` is inclusive: expiry-day-equals-event-day still carries the gap.
"""

from datetime import date, timedelta
from unittest.mock import Mock

import pytest

from src.api.market_data import MarketDataManager
from src.utils.config import Config


TODAY = date(2026, 8, 3)
EARNINGS = TODAY + timedelta(days=5)  # D+5


def _call(option_symbol, expiry, strike=200.0, delta=0.17, mid=1.50, dte=7):
    return {
        'symbol': option_symbol,
        'underlying_symbol': 'NVDA',
        'option_type': 'call',
        'strike_price': strike,
        'expiration_date': expiry.isoformat(),
        'dte': dte,
        'delta': delta,
        'mid_price': mid,
        'bid': mid - 0.05,
        'ask': mid + 0.05,
        'last_price': mid,
        'volume': 500,
        'open_interest': 2000,
        'implied_volatility': 0.42,
    }


class _SpanFixture:
    def setup_method(self):
        self.config = Mock(spec=Config)
        self.config.min_open_interest = None  # FC-075 Phase 2: inert for the wheel
        self.config.max_spread_pct = None
        self.config.call_target_dte = 45
        self.config.call_delta_range = [0.10, 0.25]
        self.config.min_call_premium = 0.30
        self.market_data = MarketDataManager(Mock(), self.config)

    def _chain(self, calls):
        self.market_data.get_option_chain_with_analysis = Mock(
            return_value={'puts': [], 'calls': calls})

    def _stats(self, symbol='NVDA'):
        return self.market_data.last_call_rejection_stats[symbol]


class TestCallSpanFilter(_SpanFixture):
    """Tests 3, 4, 5, 8 of the plan's named list (chain-level half)."""

    def test_call_candidate_spanning_earnings_is_rejected(self):
        """Test 3. D+7 expiry against a D+5 event: rejected and counted.

        MUTATION CHECK: reverting span to a days-until window (e.g. block only
        when `days_until <= 2`) lets the D+7 candidate through, because
        days_until here is 5. This test then FAILS.
        """
        spanning = _call('NVDA260810C00200000', TODAY + timedelta(days=7))
        early = _call('NVDA260806C00200000', TODAY + timedelta(days=3), strike=205.0)
        self._chain([spanning, early])

        results = self.market_data.find_suitable_calls(
            'NVDA', min_strike_price=100.0, exclude_expiry_on_or_after=EARNINGS)

        assert [c['symbol'] for c in results] == ['NVDA260806C00200000']
        assert self._stats()['expires_into_earnings'] == 1

    def test_call_candidate_expiring_before_earnings_is_allowed(self):
        """Test 4 — the DTE-extension pin.

        MUTATION CHECK, two of them, and BOTH must be caught:
          (a) a hardcoded N=7 window blocks the symbol entirely at
              days_until = 5, so this candidate disappears;
          (b) deriving N from DTE (N = 7 here) does the same.
        Either mutation makes this test FAIL. That is the operator's explicit
        rejection of both, made executable: a candidate that expires before the
        event must stay legal at any DTE.
        """
        early = _call('NVDA260806C00200000', TODAY + timedelta(days=3))
        self._chain([early])

        results = self.market_data.find_suitable_calls(
            'NVDA', min_strike_price=100.0, exclude_expiry_on_or_after=EARNINGS)

        assert [c['symbol'] for c in results] == ['NVDA260806C00200000']
        assert self._stats()['expires_into_earnings'] == 0

    def test_a_long_dated_candidate_expiring_before_earnings_is_still_allowed(self):
        """The same pin at the far end of the DTE range, where derive-N hurts most.

        Earnings 40 days out, a 30-DTE call expiring before it. Under N=DTE
        this would be blocked (days_until 40 > 30 — allowed, actually) — but
        under any *fixed* window scaled to DTE the trade becomes progressively
        harder to justify. Span answers the only question that matters: does
        the contract's life cover the report? Here it does not.
        """
        far_earnings = TODAY + timedelta(days=40)
        candidate = _call('NVDA260902C00200000', TODAY + timedelta(days=30), dte=30)
        self._chain([candidate])

        results = self.market_data.find_suitable_calls(
            'NVDA', min_strike_price=100.0, exclude_expiry_on_or_after=far_earnings)

        assert len(results) == 1

    def test_span_boundary_expiry_on_earnings_day_is_blocked(self):
        """Test 5. `>=` not `>`: assignment resolves after the report."""
        on_the_day = _call('NVDA260808C00200000', EARNINGS)
        day_before = _call('NVDA260807C00200000', EARNINGS - timedelta(days=1),
                           strike=205.0)
        self._chain([on_the_day, day_before])

        results = self.market_data.find_suitable_calls(
            'NVDA', min_strike_price=100.0, exclude_expiry_on_or_after=EARNINGS)

        assert [c['symbol'] for c in results] == ['NVDA260807C00200000']
        assert self._stats()['expires_into_earnings'] == 1

    def test_clear_symbol_candidates_unconstrained(self):
        """Test 8. `None` is byte-identical to pre-FC-013 behaviour."""
        calls = [
            _call('NVDA260810C00200000', TODAY + timedelta(days=7)),
            _call('NVDA260806C00200000', TODAY + timedelta(days=3), strike=205.0),
        ]
        self._chain(calls)
        with_gate = self.market_data.find_suitable_calls(
            'NVDA', min_strike_price=100.0, exclude_expiry_on_or_after=None)
        gated_stats = dict(self._stats())

        self._chain(calls)
        without_param = self.market_data.find_suitable_calls(
            'NVDA', min_strike_price=100.0)

        assert [c['symbol'] for c in with_gate] == [c['symbol'] for c in without_param]
        assert len(with_gate) == 2
        assert gated_stats['expires_into_earnings'] == 0
        assert gated_stats == self._stats()

    def test_every_candidate_spanning_empties_the_result(self):
        """The shape OptionsScanner reads to write a `blocked{earnings_blackout}` row."""
        self._chain([
            _call('NVDA260810C00200000', TODAY + timedelta(days=7)),
            _call('NVDA260814C00205000', TODAY + timedelta(days=11), strike=205.0),
        ])

        results = self.market_data.find_suitable_calls(
            'NVDA', min_strike_price=100.0, exclude_expiry_on_or_after=EARNINGS)

        assert results == []
        assert self._stats()['expires_into_earnings'] == 2


class TestSpanCounterAttribution(_SpanFixture):
    """The counter must mean "a QUALIFYING strike was taken by the event"."""

    def test_a_candidate_that_would_have_failed_delta_is_not_counted_as_span(self):
        """Otherwise a symbol with no tradeable strikes at all reads as
        span-emptied, and the scanner writes `blocked{earnings_blackout}` for a
        symbol earnings had nothing to do with."""
        self._chain([
            _call('NVDA260810C00200000', TODAY + timedelta(days=7), delta=0.85),
        ])

        results = self.market_data.find_suitable_calls(
            'NVDA', min_strike_price=100.0, exclude_expiry_on_or_after=EARNINGS)

        assert results == []
        assert self._stats()['expires_into_earnings'] == 0
        assert self._stats()['delta_out_of_range'] == 1

    def test_a_candidate_below_the_cost_basis_floor_is_not_counted_as_span(self):
        self._chain([
            _call('NVDA260810C00200000', TODAY + timedelta(days=7), strike=90.0),
        ])

        self.market_data.find_suitable_calls(
            'NVDA', min_strike_price=100.0, exclude_expiry_on_or_after=EARNINGS)

        assert self._stats()['expires_into_earnings'] == 0
        assert self._stats()['below_cost_basis'] == 1

    def test_span_rejections_are_included_in_total_rejected(self):
        self._chain([
            _call('NVDA260810C00200000', TODAY + timedelta(days=7)),
        ])

        self.market_data.find_suitable_calls(
            'NVDA', min_strike_price=100.0, exclude_expiry_on_or_after=EARNINGS)

        assert self._stats()['total_rejected'] == 1


class TestExpiryCoercion(_SpanFixture):
    """The chain carries expiries as ISO strings live and in replay, but the
    parser tolerates datetimes and fixtures pass dates. One coercion, so the
    comparison can never silently compare a str to a date."""

    @pytest.mark.parametrize("expiry_value", [
        "2026-08-10",
        "2026-08-10T00:00:00",
        date(2026, 8, 10),
    ])
    def test_every_expiry_shape_is_compared_correctly(self, expiry_value):
        candidate = _call('NVDA260810C00200000', TODAY + timedelta(days=7))
        candidate['expiration_date'] = expiry_value
        self._chain([candidate])

        results = self.market_data.find_suitable_calls(
            'NVDA', min_strike_price=100.0, exclude_expiry_on_or_after=EARNINGS)

        assert results == []
        assert self._stats()['expires_into_earnings'] == 1

    def test_an_unparseable_expiry_is_rejected_not_waved_through(self):
        """A risk control must not have one path where "could not tell" means
        "sell it". Structurally unreachable today, which is exactly why."""
        candidate = _call('NVDA260810C00200000', TODAY + timedelta(days=7))
        candidate['expiration_date'] = 'not-a-date'
        self._chain([candidate])

        results = self.market_data.find_suitable_calls(
            'NVDA', min_strike_price=100.0, exclude_expiry_on_or_after=EARNINGS)

        assert results == []
        assert self._stats()['expires_into_earnings'] == 1

    def test_an_unparseable_expiry_is_untouched_when_the_gate_is_off(self):
        """No span floor, no span opinion — the default path stays inert."""
        candidate = _call('NVDA260810C00200000', TODAY + timedelta(days=7))
        candidate['expiration_date'] = 'not-a-date'
        self._chain([candidate])

        results = self.market_data.find_suitable_calls('NVDA', min_strike_price=100.0)

        assert len(results) == 1


# =========================================================================== #
# FC-078 — the 'roll' criteria profile and the roll-out horizon (T-8, T-7)
#
# Plan: docs/plans/fc-078.md §3. One filter site, two profiles. The entry
# profile must be byte-identical; a second filter implementation would be the
# FC-063 dict-boundary disease in new clothes.
# =========================================================================== #

class _RollProfileFixture:
    def setup_method(self):
        self.config = Mock(spec=Config)
        self.config.min_open_interest = None  # FC-075 Phase 2: inert for the wheel
        self.config.max_spread_pct = None
        self.config.call_target_dte = 7
        self.config.call_delta_range = [0.15, 0.25]
        self.config.min_call_premium = 0.30
        self.config.rolling_max_replacement_delta = 0.60
        self.market_data = MarketDataManager(Mock(), self.config)

    def _chain(self, calls):
        self.market_data.get_option_chain_with_analysis = Mock(
            return_value={'puts': [], 'calls': calls})

    def _stats(self, symbol='NVDA'):
        return self.market_data.last_call_rejection_stats[symbol]


class TestTheEntryProfileIsInert(_RollProfileFixture):
    """T-8. *Catches:* the roll profile leaking into the scanner path.

    The scanner is the production entry path; anything that changes its results
    changes what the bot opens, silently.
    """

    def _mixed_chain(self):
        return [
            _call('NVDA260810C00200000', TODAY + timedelta(days=7),
                  strike=200.0, delta=0.17, mid=1.50, dte=7),
            _call('NVDA260810C00210000', TODAY + timedelta(days=7),
                  strike=210.0, delta=0.45, mid=0.20, dte=7),     # delta + premium
            _call('NVDA260901C00220000', TODAY + timedelta(days=29),
                  strike=220.0, delta=0.20, mid=2.00, dte=29),    # DTE
            _call('NVDA260810C00230000', TODAY + timedelta(days=7),
                  strike=230.0, delta=0.22, mid=0.10, dte=7),     # premium
        ]

    def test_the_default_call_is_byte_identical_to_passing_entry_explicitly(self):
        self._chain(self._mixed_chain())
        implicit = self.market_data.find_suitable_calls('NVDA', min_strike_price=100.0)

        self._chain(self._mixed_chain())
        explicit = self.market_data.find_suitable_calls(
            'NVDA', min_strike_price=100.0, criteria_profile='entry')

        assert implicit == explicit

    def test_the_entry_profile_still_applies_every_entry_criterion(self):
        """The fixture is built so each rejected contract is rejected for a
        DIFFERENT reason. If any one of them starts passing, an entry criterion
        has been dropped."""
        self._chain(self._mixed_chain())

        results = self.market_data.find_suitable_calls('NVDA', min_strike_price=100.0)

        assert [c['strike_price'] for c in results] == [200.0]
        stats = self._stats()
        assert stats['dte_too_high'] == 1
        assert stats['premium_too_low'] == 2      # the 0.20 and the 0.10
        assert stats['delta_out_of_range'] == 0   # premium is checked first

    def test_the_horizon_parameter_is_inert_by_default(self):
        self._chain(self._mixed_chain())
        results = self.market_data.find_suitable_calls('NVDA', min_strike_price=100.0)

        assert self._stats()['expiry_beyond_horizon'] == 0
        assert len(results) == 1

    def test_the_entry_profile_still_returns_at_most_five(self):
        """The top-5 slice is entry behaviour and must survive: only the roll
        profile returns the full legal set."""
        self._chain([
            _call(f'NVDA260810C{int(s * 1000):08d}', TODAY + timedelta(days=7),
                  strike=s, delta=0.20, mid=1.50, dte=7)
            for s in (200.0, 205.0, 210.0, 215.0, 220.0, 225.0, 230.0)
        ])

        results = self.market_data.find_suitable_calls('NVDA', min_strike_price=100.0)

        assert len(results) == 5


class TestTheRollProfile(_RollProfileFixture):

    def _roll_chain(self):
        return [
            # Past the entry DTE cap: legal on a roll, the whole point.
            _call('NVDA260901C00210000', TODAY + timedelta(days=29),
                  strike=210.0, delta=0.45, mid=5.00, dte=29),
            # Below the entry delta band's LOWER bound is fine; above the rail
            # is not.
            _call('NVDA260901C00205000', TODAY + timedelta(days=29),
                  strike=205.0, delta=0.70, mid=8.00, dte=29),
            # Sub-min_call_premium: legal on a roll.
            _call('NVDA260901C00240000', TODAY + timedelta(days=29),
                  strike=240.0, delta=0.05, mid=0.10, dte=29),
        ]

    def test_the_roll_profile_drops_dte_and_premium_and_keeps_a_delta_rail(self):
        self._chain(self._roll_chain())

        results = self.market_data.find_suitable_calls(
            'NVDA', min_strike_price=100.0, criteria_profile='roll')

        strikes = sorted(c['strike_price'] for c in results)
        assert strikes == [210.0, 240.0], "delta rail or a dropped gate is wrong"
        assert self._stats()['delta_out_of_range'] == 1   # the 0.70
        assert self._stats()['dte_too_high'] == 0
        assert self._stats()['premium_too_low'] == 0

    def test_the_roll_profile_keeps_the_liquidity_check(self):
        illiquid = _call('NVDA260901C00210000', TODAY + timedelta(days=29),
                         strike=210.0, delta=0.45, mid=5.00, dte=29)
        illiquid['volume'] = 0
        illiquid['open_interest'] = 3
        self._chain([illiquid])

        results = self.market_data.find_suitable_calls(
            'NVDA', min_strike_price=100.0, criteria_profile='roll')

        assert results == []
        assert self._stats()['no_liquidity'] == 1

    def test_the_horizon_ceiling_rejects_expiries_past_the_bound(self):
        horizon = TODAY + timedelta(days=14)
        self._chain([
            _call('NVDA260817C00210000', horizon, strike=210.0, delta=0.45,
                  mid=5.00, dte=14),
            _call('NVDA260818C00215000', horizon + timedelta(days=1),
                  strike=215.0, delta=0.45, mid=5.00, dte=15),
        ])

        results = self.market_data.find_suitable_calls(
            'NVDA', min_strike_price=100.0, criteria_profile='roll',
            include_expiry_on_or_before=horizon)

        assert [c['strike_price'] for c in results] == [210.0]
        assert self._stats()['expiry_beyond_horizon'] == 1

    def test_an_unparseable_expiry_fails_closed_against_the_horizon(self):
        candidate = _call('NVDA260817C00210000', TODAY + timedelta(days=14),
                          strike=210.0, delta=0.45, mid=5.00, dte=14)
        candidate['expiration_date'] = 'not-a-date'
        self._chain([candidate])

        results = self.market_data.find_suitable_calls(
            'NVDA', min_strike_price=100.0, criteria_profile='roll',
            include_expiry_on_or_before=TODAY + timedelta(days=14))

        assert results == []
        assert self._stats()['expiry_beyond_horizon'] == 1

    def test_the_span_floor_and_the_horizon_ceiling_compose(self):
        """The roll path is the only caller that passes both. Neither may
        cancel the other."""
        horizon = TODAY + timedelta(days=14)
        earnings = TODAY + timedelta(days=10)
        self._chain([
            _call('NVDA260810C00210000', TODAY + timedelta(days=7),
                  strike=210.0, delta=0.45, mid=5.00, dte=7),    # in, before
            _call('NVDA260814C00215000', TODAY + timedelta(days=11),
                  strike=215.0, delta=0.45, mid=5.00, dte=11),   # in, spans
            _call('NVDA260901C00220000', TODAY + timedelta(days=29),
                  strike=220.0, delta=0.45, mid=5.00, dte=29),   # out
        ])

        results = self.market_data.find_suitable_calls(
            'NVDA', min_strike_price=100.0, criteria_profile='roll',
            exclude_expiry_on_or_after=earnings,
            include_expiry_on_or_before=horizon)

        assert [c['strike_price'] for c in results] == [210.0]
        assert self._stats()['expires_into_earnings'] == 1
        assert self._stats()['expiry_beyond_horizon'] == 1

    def test_the_roll_profile_returns_the_full_legal_set_not_a_top_five_slice(self):
        """FC-078 DD-3 ranks by NET CREDIT, and ``return_score`` (annualised
        yield x OTM preference) is entry-shaped. Truncating by it would drop
        near-money, high-credit replacements before the roller ever ranked them
        — the same failure mode DD-3 rejected first-past-the-post ordering for.
        """
        self._chain([
            _call(f'NVDA260901C{int(s * 1000):08d}', TODAY + timedelta(days=29),
                  strike=s, delta=0.45, mid=5.00, dte=29)
            for s in (205.0, 210.0, 215.0, 220.0, 225.0, 230.0, 235.0)
        ])

        results = self.market_data.find_suitable_calls(
            'NVDA', min_strike_price=100.0, criteria_profile='roll')

        assert len(results) == 7

    def test_an_unknown_profile_is_refused_loudly(self):
        self._chain([])
        with pytest.raises(ValueError, match='criteria_profile'):
            self.market_data.find_suitable_calls(
                'NVDA', min_strike_price=100.0, criteria_profile='rol')
