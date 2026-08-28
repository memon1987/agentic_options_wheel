"""Unit tests for the shared sell-to-open limit pricer (FC-072 rev 2).

The seller tests pin the behavior through each leg. These pin the pure
functions underneath, where the degenerate inputs are cheap to enumerate: this
module is the one place in the system that turns a broker quote into a price we
are willing to be filled at, and it runs on every order both legs place.

Three families here are not "coverage" — each is a defect that shipped or was
argued for and rejected:

* **Tick increments** ($0.05 at/above $3.00) are a broker rule. Off-tick limits
  are rejected live when no exchange accepts them; the paper simulator does not
  enforce them, so paper history cannot clear this.
* **Half-cent rounding** must be ROUND_HALF_UP on the exact decimal. `round()`
  is banker's rounding over binary floats, which put a half-cent mid on the bid
  or the ask by luck.
* **Locked and wide books.** A locked book prices at the bid, never 5% through
  a market that is quoting. A wide book is illiquid, not stale, and is priced
  normally — rev 1's spread cap is deliberately gone.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock

import pytest

from pathlib import Path

import yaml

from src.api.alpaca_client import OPTION_QUOTE_FEED
from src.strategy import limit_pricing
from src.strategy.limit_pricing import (
    ALWAYS_PENNY_SYMBOLS,
    FALLBACK_PREMIUM_DISCOUNT,
    VERIFIED_PENNY_PROGRAM_ROOTS,
    _WARNED_UNVERIFIED_ROOTS,
    executing_quote_fields,
    pricing_log_fields,
    quote_age_seconds,
    quote_is_usable,
    refresh_quote,
    round_to_tick,
    sell_limit_price,
    tick_size,
)
from src.utils import clock

REPO = Path(__file__).resolve().parent.parent


class TestQuoteIsUsable:
    def test_a_normal_two_sided_book_is_usable(self):
        assert quote_is_usable(1.20, 1.40) is True

    def test_a_locked_book_is_usable(self):
        """ask == bid is a market — the only price being shown. Falling back
        would price 5% THROUGH it."""
        assert quote_is_usable(1.30, 1.30) is True

    def test_a_wide_book_is_usable_and_uncapped(self):
        """spread/mid = 0.46/0.91 = 0.51. Rev 1 capped this and conceded 5%;
        wide means illiquid, not stale."""
        assert quote_is_usable(0.68, 1.14) is True

    def test_an_absurdly_wide_book_is_still_usable(self):
        """There is deliberately no width at which the predicate gives up."""
        assert quote_is_usable(0.05, 5.00) is True

    @pytest.mark.parametrize("bid,ask", [
        (0, 0),            # the scanner's default for an absent quote
        (0, 1.40),         # one-sided: no bid
        (1.20, 0),         # one-sided: no ask
        (None, None),
        (1.20, None),
        (None, 1.40),
        (1.40, 1.30),      # crossed
        (-1.0, 1.40),
    ])
    def test_one_sided_crossed_and_missing_books_are_unusable(self, bid, ask):
        assert quote_is_usable(bid, ask) is False

    @pytest.mark.parametrize("value", ['1.20', True, False, [1.2], {}])
    def test_non_numeric_quote_fields_are_unusable(self, value):
        """`True` is the sharp one: bool is an int subclass, so an unguarded
        numeric check reads a stray True as a $1.00 bid."""
        assert quote_is_usable(value, 1.40) is False
        assert quote_is_usable(1.20, value) is False


class TestRoundToTick:
    """Alpaca: $0.01 below $3.00, $0.05 at/above, penny program exempt."""

    def test_at_or_above_three_dollars_snaps_up_to_the_nickel(self):
        assert round_to_tick(Decimal("3.24"), "AAPL") == (3.25, True)

    def test_a_price_already_on_the_nickel_is_not_moved(self):
        assert round_to_tick(Decimal("3.25"), "AAPL") == (3.25, False)

    def test_it_never_rounds_a_sell_limit_down(self):
        """Rounding down would hand the buyer up to a tick for nothing."""
        for raw in ("3.01", "3.19", "3.44", "14.41"):
            price, _ = round_to_tick(Decimal(raw), "AAPL")
            assert price >= float(raw)
            assert price - float(raw) < 0.05

    @pytest.mark.parametrize("symbol", sorted(ALWAYS_PENNY_SYMBOLS))
    def test_always_penny_names_keep_the_cent_above_three_dollars(self, symbol):
        """SPY/QQQ/IWM are the standing exception to the $3.00 rule."""
        assert round_to_tick(Decimal("3.24"), symbol) == (3.24, False)

    def test_the_always_penny_check_is_case_and_space_insensitive(self):
        assert round_to_tick(Decimal("3.24"), " spy ") == (3.24, False)

    def test_an_unknown_symbol_gets_the_penny_program_rule(self):
        """Not knowing the underlying must not produce an illegal limit. The
        empty root is the "no underlying on the opportunity" case and warns
        nowhere, because there is no root to name."""
        assert round_to_tick(Decimal("3.24"), "") == (3.25, True)

    def test_below_three_dollars_rounds_to_the_cent(self):
        assert round_to_tick(Decimal("1.234"), "AAPL") == (1.23, False)

    def test_the_boundary_case_rounds_half_up_onto_a_legal_tick(self):
        """2.995 is a sub-$3 price, so it takes the cent rule — and half-up
        lands on 3.00, which is itself on a $0.05 tick. Nothing is lost by not
        re-testing the threshold after rounding."""
        assert round_to_tick(Decimal("2.995"), "AAPL") == (3.00, False)

    def test_half_cents_round_up_not_to_even(self):
        """`round(0.605, 2)` is 0.6 — banker's rounding over a float that is
        really 0.60499999. Half-up puts the order on the ask, where it rests."""
        assert round_to_tick(Decimal("0.605"), "AAPL")[0] == 0.61
        assert round_to_tick(Decimal("1.615"), "AAPL")[0] == 1.62
        assert round_to_tick(Decimal("2.985"), "AAPL")[0] == 2.99

    def test_tick_snapped_is_false_when_the_nickel_rule_changed_nothing(self):
        """The flag reports that the price MOVED, so a log reader can tell a
        snap from a coincidence."""
        assert round_to_tick(Decimal("3.10"), "AAPL")[1] is False
        assert round_to_tick(Decimal("3.11"), "AAPL")[1] is True


class TestQuoteAgeSeconds:
    def test_it_measures_a_naive_stamp_against_naive_now(self):
        """`scan_timestamp` comes from `clock.now()`, which is naive. Comparing
        it against an aware `now` raises — that is the bug this guards."""
        stamp = (clock.now() - timedelta(seconds=900)).isoformat()
        age = quote_age_seconds(stamp)
        assert age is not None and 890 <= age <= 910

    def test_it_measures_an_aware_stamp_against_aware_now(self):
        stamp = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        age = quote_age_seconds(stamp)
        assert age is not None and 55 <= age <= 65

    def test_a_z_suffixed_stamp_parses(self):
        stamp = datetime.now(timezone.utc).replace(
            microsecond=0).isoformat().replace("+00:00", "Z")
        assert quote_age_seconds(stamp) is not None

    def test_it_accepts_a_datetime_directly(self):
        assert quote_age_seconds(clock.now() - timedelta(seconds=30)) >= 25

    @pytest.mark.parametrize("bad", [None, "", "   ", "not-a-date", 12345, {}])
    def test_an_unreadable_stamp_is_none_not_an_exception(self, bad):
        """Age is a log field, never an order decision — it must not be able to
        fail a write."""
        assert quote_age_seconds(bad) is None

    def test_clock_skew_is_reported_rather_than_clamped(self):
        future = (clock.now() + timedelta(seconds=120)).isoformat()
        assert quote_age_seconds(future) < 0


class TestRefreshQuote:
    """The execute-time re-quote. `/scan` at :00, `/run` at :15."""

    @staticmethod
    def _opportunity(**kw):
        opp = {
            'symbol': 'AAPL',
            'bid': 1.20,
            'ask': 1.40,
            'premium': 1.30,
            'scan_timestamp': (clock.now() - timedelta(seconds=900)).isoformat(),
        }
        opp.update(kw)
        return opp

    def test_a_usable_live_quote_wins_over_the_blob(self):
        alpaca = Mock()
        alpaca.get_option_quote.return_value = {'bid': 1.30, 'ask': 1.50}

        quote = refresh_quote(alpaca, 'AAPL260821C00310000', self._opportunity())

        assert (quote.bid, quote.ask) == (1.30, 1.50)
        assert quote.source == "live"
        assert quote.age_s is None
        alpaca.get_option_quote.assert_called_once_with('AAPL260821C00310000')

    def test_an_empty_refresh_falls_back_to_the_blob_with_its_age(self):
        alpaca = Mock()
        alpaca.get_option_quote.return_value = {}

        quote = refresh_quote(alpaca, 'AAPL260821C00310000', self._opportunity())

        assert (quote.bid, quote.ask) == (1.20, 1.40)
        assert quote.source == "blob"
        assert 890 <= quote.age_s <= 910

    def test_a_raising_refresh_never_fails_the_order(self):
        """A refused order costs a whole cycle of theta; a stale quote costs a
        few cents. The fallback is not optional."""
        alpaca = Mock()
        alpaca.get_option_quote.side_effect = RuntimeError("boom")

        quote = refresh_quote(alpaca, 'AAPL260821C00310000', self._opportunity())

        assert quote.source == "blob"
        assert quote.bid == 1.20

    def test_a_one_sided_live_quote_is_not_live(self):
        """A quote that came back is not the same as a quote we can price off."""
        alpaca = Mock()
        alpaca.get_option_quote.return_value = {'bid': 0.0, 'ask': 1.50}

        quote = refresh_quote(alpaca, 'AAPL260821C00310000', self._opportunity())

        assert quote.source == "blob"

    def test_a_crossed_live_quote_is_not_live(self):
        alpaca = Mock()
        alpaca.get_option_quote.return_value = {'bid': 1.50, 'ask': 1.40}
        assert refresh_quote(alpaca, 'X', self._opportunity()).source == "blob"

    def test_a_missing_scan_stamp_gives_a_none_age_not_a_crash(self):
        alpaca = Mock()
        alpaca.get_option_quote.return_value = {}
        opp = self._opportunity()
        del opp['scan_timestamp']

        quote = refresh_quote(alpaca, 'X', opp)

        assert quote.source == "blob"
        assert quote.age_s is None

    def test_the_failure_is_logged_for_the_two_week_readout(self):
        alpaca = Mock()
        alpaca.get_option_quote.side_effect = RuntimeError("boom")
        log = Mock()

        refresh_quote(alpaca, 'AAPL260821C00310000', self._opportunity(), log)

        assert log.warning.call_count == 1
        kwargs = log.warning.call_args.kwargs
        assert kwargs['event_type'] == 'quote_refresh_failed'
        assert kwargs['reason'] == 'api_error'

    def test_a_successful_refresh_logs_nothing(self):
        alpaca = Mock()
        alpaca.get_option_quote.return_value = {'bid': 1.30, 'ask': 1.50}
        log = Mock()

        refresh_quote(alpaca, 'X', self._opportunity(), log)

        log.warning.assert_not_called()


class TestSellLimitPrice:
    def test_a_zero_fraction_rests_at_mid(self):
        priced = sell_limit_price(1.30, 1.50, 1.30, 0.0, "AAPL")
        assert priced.limit_price == 1.40
        assert priced.mid == 1.40
        assert priced.spread == pytest.approx(0.20)
        assert priced.spread_fraction == 0.0
        assert priced.tick_snapped is False

    def test_a_positive_fraction_biases_toward_the_ask(self):
        """The put leg's historical formula, on a book whose mid IS 1.44:
        1.44 + 0.10 * (1.48 - 1.40) = 1.448 -> 1.45 half-up."""
        assert sell_limit_price(1.40, 1.48, 1.44, 0.10, "AAPL").limit_price == 1.45

    def test_the_full_fraction_lands_on_the_ask(self):
        assert sell_limit_price(1.30, 1.50, 1.30, 0.5, "AAPL").limit_price == 1.50

    def test_a_locked_book_prices_at_the_bid_on_both_settings(self):
        """Zero spread, so the fraction cannot move it — and it must not fall
        back to 5% through the market."""
        for fraction in (0.0, 0.10, 0.5):
            priced = sell_limit_price(1.30, 1.30, 1.30, fraction, "AAPL")
            assert priced.limit_price == 1.30
            assert priced.spread == 0.0
            assert priced.spread_fraction == fraction

    def test_a_wide_book_takes_the_normal_formula(self):
        """The real AMZN 2026-05-27 book. Rev 1's cap priced this 0.86; the put
        formula prices 0.96, and that is the intended behavior."""
        priced = sell_limit_price(0.68, 1.14, 0.91, 0.10, "AMZN")
        assert priced.limit_price == 0.96
        assert priced.spread_fraction == 0.10

    def test_mid_is_recomputed_from_the_book_not_taken_from_the_premium(self):
        """The whole point of the re-quote: a fresh book must not be priced
        off a stale premium."""
        priced = sell_limit_price(1.30, 1.50, 999.0, 0.0, "AAPL")
        assert priced.limit_price == 1.40

    @pytest.mark.parametrize("bid,ask", [(0, 0), (0, 1.4), (1.4, 0),
                                         (None, None), (1.5, 1.4)])
    def test_an_unusable_book_takes_the_historical_premium_fallback(self, bid, ask):
        priced = sell_limit_price(bid, ask, 1.30, 0.10, "AAPL")
        assert priced.limit_price == 1.24  # round(1.30 * 0.95, 2)
        assert priced.mid is None
        assert priced.spread_fraction is None, "None marks the fallback in logs"

    def test_the_fallback_ignores_the_fraction_entirely(self):
        """There is deliberately no third path."""
        for fraction in (0.0, 0.10, 0.5):
            assert sell_limit_price(0, 0, 1.30, fraction, "AAPL").limit_price == 1.24

    def test_the_fallback_is_tick_snapped_too(self):
        """An off-tick limit is rejected whichever branch produced it.
        round(3.40 * 0.95, 2) = 3.23 -> 3.25."""
        priced = sell_limit_price(0, 0, 3.40, 0.0, "AAPL")
        assert priced.limit_price == 3.25
        assert priced.tick_snapped is True

    def test_the_fallback_reports_the_book_it_refused(self):
        """A crossed book is still worth seeing in the logs."""
        priced = sell_limit_price(1.50, 1.40, 1.30, 0.0, "AAPL")
        assert priced.spread == pytest.approx(-0.10)

    def test_a_priced_limit_above_three_dollars_is_snapped(self):
        priced = sell_limit_price(3.14, 3.34, 3.24, 0.0, "AAPL")
        assert priced.limit_price == 3.25
        assert priced.mid == 3.24
        assert priced.tick_snapped is True

    def test_a_penny_program_limit_above_three_dollars_is_not_snapped(self):
        priced = sell_limit_price(3.14, 3.34, 3.24, 0.0, "SPY")
        assert priced.limit_price == 3.24
        assert priced.tick_snapped is False

    def test_a_one_tick_book_prices_at_the_bid_not_the_ask(self):
        """bid 0.60 / ask 0.61 is ONE TICK wide: there is no midpoint to rest
        at, and "mid rounded half-up" is just the ask. Resting at the ask on a
        low-delta call costs more in missed cycles than it captures."""
        priced = sell_limit_price(0.60, 0.61, 0.605, 0.0, "AAPL")
        assert priced.limit_price == 0.60
        assert priced.one_tick_book is True

    def test_a_zero_premium_and_unusable_book_cannot_produce_a_negative_limit(self):
        assert sell_limit_price(0, 0, 0, 0.0, "AAPL").limit_price == 0.0
        assert sell_limit_price(0, 0, None, 0.0, "AAPL").limit_price == 0.0

    def test_the_limit_stays_inside_the_book_across_the_validated_range(self):
        """The config bound [0.0, 0.5] exists to make this true below $3.00,
        where no tick snapping applies."""
        for fraction in (0.0, 0.1, 0.25, 0.5):
            priced = sell_limit_price(1.30, 1.50, 1.40, fraction, "AAPL")
            assert 1.40 <= priced.limit_price <= 1.50

    def test_the_limit_is_never_below_mid_on_a_usable_book(self):
        for bid, ask in [(0.30, 0.40), (1.75, 1.85), (3.14, 3.34), (14.20, 14.62)]:
            priced = sell_limit_price(bid, ask, 0.0, 0.0, "AAPL")
            assert priced.limit_price >= (bid + ask) / 2

    def test_the_fallback_discount_constant_is_the_historical_one(self):
        assert FALLBACK_PREMIUM_DISCOUNT == Decimal("0.95")


class TestTickTaxonomyFC072Review:
    """N3: the two increment regimes, and the one this code cannot emit.

    The rev-2 naming taught the wrong taxonomy. "Penny program" is the RULE
    ($0.01 below $3.00, $0.05 at and above) that every one of the 14 configured
    roots was verified to follow (`ppind=True`, 2026-08-28). SPY/QQQ/IWM are
    the *always-penny* exception to the $3.00 half of it. A genuine non-penny
    class ticks $0.05/$0.10 — increments this module deliberately cannot
    produce, because no such root is tradeable here and an untested third
    regime would be worse than a loud warning.
    """

    def setup_method(self):
        _WARNED_UNVERIFIED_ROOTS.clear()

    def teardown_method(self):
        _WARNED_UNVERIFIED_ROOTS.clear()

    def test_every_configured_root_is_recorded_as_verified(self):
        """The census is the point: if a symbol is added to the universe and
        not verified here, it prices under an ASSUMPTION."""
        with open(REPO / 'config' / 'settings.yaml') as fh:
            configured = set(yaml.safe_load(fh)['stocks']['symbols'])
        assert configured <= VERIFIED_PENNY_PROGRAM_ROOTS, (
            "configured roots missing from the verified set: "
            f"{sorted(configured - VERIFIED_PENNY_PROGRAM_ROOTS)}")

    def test_the_always_penny_names_are_also_in_the_verified_set(self):
        """They are configured symbols too; the sets overlap by design."""
        assert ALWAYS_PENNY_SYMBOLS <= VERIFIED_PENNY_PROGRAM_ROOTS

    @pytest.mark.parametrize("price", ["0.50", "2.99", "3.00", "3.24", "14.41"])
    def test_always_penny_roots_tick_at_a_cent_everywhere(self, price):
        assert tick_size(Decimal(price), "SPY") == Decimal("0.01")

    @pytest.mark.parametrize("price,expected", [
        ("0.50", "0.01"), ("2.99", "0.01"), ("3.00", "0.05"), ("3.24", "0.05"),
    ])
    def test_verified_roots_switch_at_three_dollars(self, price, expected):
        assert tick_size(Decimal(price), "AAPL") == Decimal(expected)

    def test_an_unverified_root_is_priced_under_the_same_rule(self):
        """Assume, price, and shout — do not refuse the order."""
        assert round_to_tick(Decimal("3.24"), "TSLA") == (3.25, True)

    def test_an_unverified_root_warns_once_per_process(self, monkeypatch):
        warnings = []
        monkeypatch.setattr(
            limit_pricing.logger, "warning",
            lambda *a, **kw: warnings.append(kw))

        for _ in range(5):
            round_to_tick(Decimal("3.24"), "TSLA")
            sell_limit_price(3.14, 3.34, 3.24, 0.0, "TSLA")

        assert len(warnings) == 1, "one line per process per root, not per order"
        assert warnings[0]['event_type'] == 'tick_class_unverified'
        assert warnings[0]['symbol'] == 'TSLA'

    def test_each_unverified_root_gets_its_own_warning(self, monkeypatch):
        warnings = []
        monkeypatch.setattr(
            limit_pricing.logger, "warning",
            lambda *a, **kw: warnings.append(kw))

        for root in ("TSLA", "META", "TSLA"):
            round_to_tick(Decimal("3.24"), root)

        assert [w['symbol'] for w in warnings] == ['TSLA', 'META']

    @pytest.mark.parametrize("root", ["AAPL", "SPY", ""])
    def test_a_verified_or_absent_root_never_warns(self, root, monkeypatch):
        warnings = []
        monkeypatch.setattr(
            limit_pricing.logger, "warning",
            lambda *a, **kw: warnings.append(kw))
        round_to_tick(Decimal("3.24"), root)
        assert warnings == []


class TestOneTickBooksFC072Review:
    """N4: a one-tick book has no midpoint, so it prices at the bid.

    "Mid, rounded half-up" on a one-tick book IS the ask. Resting at the ask on
    a 0.15-0.25 delta call costs more in missed cycles than it captures — on
    the journal sample roughly -$30 of forgone cycles against +$6 of spread.
    """

    def test_a_one_cent_book_below_three_dollars_prices_at_the_bid(self):
        priced = sell_limit_price(0.60, 0.61, 0.605, 0.0, "AAPL")
        assert priced.limit_price == 0.60
        assert priced.one_tick_book is True

    def test_a_five_cent_book_above_three_dollars_prices_at_the_bid(self):
        """3.05/3.10 on a penny-program root is one tick at that price level."""
        priced = sell_limit_price(3.05, 3.10, 3.075, 0.0, "AAPL")
        assert priced.limit_price == 3.05
        assert priced.one_tick_book is True

    def test_a_two_tick_book_keeps_the_midpoint(self):
        priced = sell_limit_price(0.60, 0.62, 0.61, 0.0, "AAPL")
        assert priced.limit_price == 0.61
        assert priced.one_tick_book is False

    def test_a_one_cent_book_above_three_dollars_is_NOT_one_tick_for_aapl(self):
        """On a penny-program root at $3+, one tick is $0.05 — a $0.01 book is
        a sub-tick quote, so the normal formula runs (and snaps up)."""
        priced = sell_limit_price(3.20, 3.21, 3.205, 0.0, "AAPL")
        assert priced.one_tick_book is False
        assert priced.limit_price == 3.25

    def test_a_one_cent_book_above_three_dollars_IS_one_tick_for_spy(self):
        """SPY is penny at every level, so $0.01 is one tick even at $3+."""
        priced = sell_limit_price(3.20, 3.21, 3.205, 0.0, "SPY")
        assert priced.one_tick_book is True
        assert priced.limit_price == 3.20

    def test_the_spread_fraction_cannot_override_a_one_tick_book(self):
        """There is nowhere for the bias to go: the ask is one tick away."""
        for fraction in (0.0, 0.10, 0.5):
            priced = sell_limit_price(0.60, 0.61, 0.605, fraction, "AAPL")
            assert priced.limit_price == 0.60
            assert priced.spread_fraction == fraction, (
                "the configured fraction is still reported, so a log reader can "
                "see what one_tick_book overrode")

    def test_a_locked_book_is_not_a_one_tick_book(self):
        """Zero spread is narrower than one tick; it prices at the bid anyway,
        by the normal formula, and must not be mislabelled."""
        priced = sell_limit_price(1.30, 1.30, 1.30, 0.0, "AAPL")
        assert priced.limit_price == 1.30
        assert priced.one_tick_book is False

    def test_the_one_tick_rule_reports_the_real_mid_for_telemetry(self):
        """The price is the bid, but the readout still needs the midpoint it
        declined to rest at — that is the (fill - mid) the study measures."""
        priced = sell_limit_price(0.60, 0.61, 0.605, 0.0, "AAPL")
        assert priced.mid == 0.605


class TestFallbackReasonsFC072Review:
    """N2: a book we REFUSED must not look like a broker that never answered."""

    @staticmethod
    def _opportunity():
        return {'symbol': 'AAPL', 'bid': 1.20, 'ask': 1.40, 'premium': 1.30,
                'scan_timestamp': clock.now().isoformat()}

    def test_a_raising_refresh_reports_api_error(self):
        alpaca = Mock()
        alpaca.get_option_quote.side_effect = RuntimeError("boom")
        quote = refresh_quote(alpaca, 'X', self._opportunity())
        assert quote.fallback_reason == 'api_error'

    def test_an_empty_refresh_reports_empty(self):
        alpaca = Mock()
        alpaca.get_option_quote.return_value = {}
        assert refresh_quote(alpaca, 'X', self._opportunity()).fallback_reason == 'empty'

    @pytest.mark.parametrize("bid,ask", [(0.0, 1.50), (1.30, 0.0), (1.50, 1.40)])
    def test_a_one_sided_or_crossed_live_book_reports_unusable_live_book(
            self, bid, ask):
        """THE gap this closes: rev 2 fell back silently here."""
        alpaca = Mock()
        alpaca.get_option_quote.return_value = {'bid': bid, 'ask': ask}
        quote = refresh_quote(alpaca, 'X', self._opportunity())
        assert quote.fallback_reason == 'unusable_live_book'

    def test_the_refused_book_is_named_in_the_warning(self):
        """Without live_bid/live_ask the log says a book was refused but not
        which one, and the readout cannot tell a stale feed from a real
        one-sided market."""
        alpaca = Mock()
        alpaca.get_option_quote.return_value = {'bid': 0.0, 'ask': 1.50}
        log = Mock()

        refresh_quote(alpaca, 'AAPL260821C00310000', self._opportunity(), log)

        kwargs = log.warning.call_args.kwargs
        assert kwargs['event_type'] == 'quote_refresh_failed'
        assert kwargs['reason'] == 'unusable_live_book'
        assert kwargs['live_bid'] == 0.0
        assert kwargs['live_ask'] == 1.50

    def test_a_live_quote_has_no_fallback_reason(self):
        alpaca = Mock()
        alpaca.get_option_quote.return_value = {'bid': 1.30, 'ask': 1.50}
        assert refresh_quote(alpaca, 'X', self._opportunity()).fallback_reason is None


class TestLiveQuoteProvenanceFC072Review:
    """N5/N7: which feed the book came from, and how old it really is."""

    @staticmethod
    def _opportunity():
        return {'symbol': 'AAPL', 'bid': 1.20, 'ask': 1.40, 'premium': 1.30,
                'scan_timestamp': clock.now().isoformat()}

    def test_the_feed_is_carried_through_from_the_client(self):
        alpaca = Mock()
        alpaca.get_option_quote.return_value = {
            'bid': 1.30, 'ask': 1.50, 'feed': 'indicative'}
        assert refresh_quote(alpaca, 'X', self._opportunity()).feed == 'indicative'

    def test_the_client_asks_for_the_feed_explicitly(self):
        """The constant is the audit trail for "these are not NBBO quotes"."""
        assert OPTION_QUOTE_FEED == 'indicative'

    def test_a_live_quotes_age_comes_from_the_brokers_stamp(self):
        """N7: a halted or thin name's "latest" quote can be hours old.
        Reporting 0.0 would launder that into apparent freshness."""
        stale = datetime.now(timezone.utc) - timedelta(seconds=3600)
        alpaca = Mock()
        alpaca.get_option_quote.return_value = {
            'bid': 1.30, 'ask': 1.50, 'timestamp': stale}

        quote = refresh_quote(alpaca, 'X', self._opportunity())

        assert quote.source == 'live'
        assert 3590 <= quote.age_s <= 3610, "an hour-old book is not fresh"

    def test_a_genuinely_fresh_live_quote_reads_as_fresh(self):
        alpaca = Mock()
        alpaca.get_option_quote.return_value = {
            'bid': 1.30, 'ask': 1.50, 'timestamp': datetime.now(timezone.utc)}
        assert refresh_quote(alpaca, 'X', self._opportunity()).age_s < 5

    def test_a_live_quote_without_a_stamp_reports_an_unknown_age(self):
        """None, not 0.0: an age we cannot establish must not read as fresh."""
        alpaca = Mock()
        alpaca.get_option_quote.return_value = {'bid': 1.30, 'ask': 1.50}
        assert refresh_quote(alpaca, 'X', self._opportunity()).age_s is None

    def test_the_live_stamp_is_carried_as_a_string(self):
        stamp = datetime.now(timezone.utc)
        alpaca = Mock()
        alpaca.get_option_quote.return_value = {
            'bid': 1.30, 'ask': 1.50, 'timestamp': stamp}
        assert refresh_quote(alpaca, 'X', self._opportunity()).ts == stamp.isoformat()

    def test_a_blob_quote_still_ages_from_the_scan_stamp(self):
        alpaca = Mock()
        alpaca.get_option_quote.return_value = {}
        opp = self._opportunity()
        opp['scan_timestamp'] = (clock.now() - timedelta(seconds=900)).isoformat()

        quote = refresh_quote(alpaca, 'X', opp)

        assert quote.source == 'blob'
        assert 890 <= quote.age_s <= 910
        assert quote.ts is None


class TestSharedLogFieldSetsFC072Review:
    """One field set, both legs. A field on one leg only halves the readout."""

    @staticmethod
    def _quote_and_price():
        alpaca = Mock()
        alpaca.get_option_quote.return_value = {
            'bid': 1.30, 'ask': 1.50, 'feed': 'indicative',
            'timestamp': datetime.now(timezone.utc)}
        quote = refresh_quote(alpaca, 'X', {'symbol': 'AAPL', 'bid': 1.20,
                                            'ask': 1.40, 'premium': 1.30})
        priced = sell_limit_price(quote.bid, quote.ask, 1.30, 0.0, "AAPL")
        return quote, priced

    def test_the_log_field_set_is_complete(self):
        quote, priced = self._quote_and_price()
        fields = pricing_log_fields(quote, priced)
        assert set(fields) == {
            'bid', 'ask', 'mid', 'spread', 'spread_fraction', 'quote_source',
            'quote_age_s', 'quote_fallback_reason', 'quote_feed', 'quote_ts',
            'tick_snapped', 'one_tick_book', 'limit_price'}

    def test_the_journal_field_set_is_the_executing_book(self):
        quote, priced = self._quote_and_price()
        fields = executing_quote_fields(quote, priced)
        assert set(fields) == {'bid', 'ask', 'mid', 'quote_source',
                               'quote_age_s', 'tick_snapped'}
        assert (fields['bid'], fields['ask'], fields['mid']) == (1.30, 1.50, 1.40)
