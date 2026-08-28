"""Unit tests for the shared sell-to-open limit pricer (FC-072).

The seller tests in ``test_call_seller.py`` / ``test_put_seller.py`` pin the
behavior through each leg. These pin the pure function underneath them, where
the degenerate inputs are cheap to enumerate: this is the one place in the
system that turns a broker quote into a price we are willing to be filled at,
and it is called on every order.
"""

import pytest

from src.strategy.limit_pricing import (
    FALLBACK_MID_DISCOUNT,
    MAX_SPREAD_TO_MID_RATIO,
    quote_is_usable,
    quote_spread,
    sell_limit_price,
)


class TestQuoteIsUsable:
    def test_a_normal_two_sided_quote_is_usable(self):
        assert quote_is_usable(3.24, 3.14, 3.34) is True

    @pytest.mark.parametrize("bid,ask", [
        (0, 0),            # scanner's default for an absent quote
        (0, 3.34),         # no bid
        (3.14, 0),         # no ask
        (None, None),
        (3.14, None),
        (None, 3.34),
        (3.34, 3.14),      # crossed
        (3.24, 3.24),      # locked
        (-1.0, 3.34),      # nonsense
    ])
    def test_degenerate_books_are_not_usable(self, bid, ask):
        assert quote_is_usable(3.24, bid, ask) is False

    @pytest.mark.parametrize("value", ['3.14', True, False, [3.14], {}])
    def test_non_numeric_quote_fields_are_not_usable(self, value):
        """`True` is the sharp one: bool is an int subclass, so an unguarded
        numeric check reads a stray True as a $1.00 bid."""
        assert quote_is_usable(3.24, value, 3.34) is False
        assert quote_is_usable(3.24, 1.0, value) is False

    @pytest.mark.parametrize("mid", [0, -1.0, None, '3.24'])
    def test_a_nonpositive_or_nonnumeric_mid_is_not_usable(self, mid):
        """Guards the division in the ratio test, and refuses to price off a
        premium the scanner could not compute."""
        assert quote_is_usable(mid, 3.14, 3.34) is False

    def test_the_cap_boundary_is_inclusive(self):
        """spread/mid == 0.5 exactly is admitted; the cap rejects only wider."""
        assert quote_is_usable(3.00, 2.25, 3.75) is True   # ratio == 0.50
        assert quote_is_usable(3.00, 2.24, 3.76) is False  # ratio == 0.5067

    def test_the_cap_constant_is_the_documented_half_of_mid(self):
        assert MAX_SPREAD_TO_MID_RATIO == 0.5


class TestQuoteSpread:
    def test_it_reports_width_for_telemetry(self):
        assert quote_spread(3.14, 3.34) == pytest.approx(0.20)

    def test_it_reports_a_crossed_book_rather_than_hiding_it(self):
        """Reported even though such a quote is unusable — an operator reading
        the logs needs to see WHY the fallback fired."""
        assert quote_spread(3.34, 3.14) == pytest.approx(-0.20)

    @pytest.mark.parametrize("bid,ask", [(None, 3.34), (3.14, None),
                                         ('a', 3.34), (True, 3.34)])
    def test_it_is_none_when_either_side_is_not_a_number(self, bid, ask):
        assert quote_spread(bid, ask) is None


class TestSellLimitPrice:
    def test_a_zero_fraction_prices_at_mid(self):
        limit, spread, applied = sell_limit_price(3.24, 3.14, 3.34, 0.0)
        assert (limit, spread, applied) == (3.24, pytest.approx(0.20), 0.0)

    def test_a_positive_fraction_biases_toward_the_ask(self):
        limit, _, applied = sell_limit_price(1.44, 1.40, 1.50, 0.10)
        assert limit == 1.45
        assert applied == 0.10

    def test_the_full_fraction_lands_on_the_ask(self):
        limit, _, _ = sell_limit_price(3.24, 3.14, 3.34, 0.5)
        assert limit == 3.34

    def test_the_fallback_is_five_percent_under_mid(self):
        limit, spread, applied = sell_limit_price(3.24, None, None, 0.10)
        assert limit == 3.08
        assert spread is None
        assert applied is None, "None marks the fallback in the telemetry"

    def test_the_fallback_ignores_the_fraction_entirely(self):
        """There is deliberately no third path: a degraded quote gets the old
        formula whatever the knob says."""
        for fraction in (0.0, 0.10, 0.5):
            assert sell_limit_price(3.24, 0, 0, fraction)[0] == 3.08

    def test_a_capped_quote_takes_the_fallback_and_reports_its_spread(self):
        limit, spread, applied = sell_limit_price(3.24, 2.24, 4.24, 0.0)
        assert limit == 3.08
        assert spread == pytest.approx(2.00)
        assert applied is None

    def test_prices_are_rounded_to_the_cent_not_to_a_five_cent_tick(self):
        """FC-072 verified against a year of closed Alpaca orders that off-tick
        option limits at or above $3.00 are accepted and fill (46 sell-call
        limits >= $3.00 were off the $0.05 tick; 38 filled, 0 rejected across
        682 option limit orders). Snapping would move real money for no
        broker-side reason, so it is deliberately absent — this test is the
        record of that decision.
        """
        limit, _, _ = sell_limit_price(3.24, 3.14, 3.34, 0.0)
        assert limit == 3.24
        limit, _, _ = sell_limit_price(14.41, 14.20, 14.62, 0.0)
        assert limit == 14.41

    def test_the_fallback_discount_constant_is_the_historical_one(self):
        assert FALLBACK_MID_DISCOUNT == 0.95

    def test_a_zero_mid_cannot_produce_a_negative_or_exploding_limit(self):
        """Defensive: a malformed opportunity must produce 0.0, never a crash
        or a nonsense price that reaches the broker."""
        assert sell_limit_price(0, 3.14, 3.34, 0.10)[0] == 0.0
        assert sell_limit_price(None, 3.14, 3.34, 0.10)[0] == 0.0

    def test_the_limit_never_exceeds_the_ask_within_the_validated_range(self):
        """The config bound [0.0, 0.5] exists to make this true. Pinned here so
        a widened bound has to confront it."""
        for fraction in (0.0, 0.1, 0.25, 0.5):
            limit, _, _ = sell_limit_price(3.24, 3.14, 3.34, fraction)
            assert 3.24 <= limit <= 3.34

    def test_the_limit_is_never_below_mid_on_a_usable_quote(self):
        """The whole point of FC-072: a usable quote never prices under mid."""
        for mid, bid, ask in [(0.35, 0.30, 0.40), (1.80, 1.75, 1.85),
                              (3.24, 3.14, 3.34), (14.41, 14.20, 14.62)]:
            limit, _, _ = sell_limit_price(mid, bid, ask, 0.0)
            assert limit >= round(mid, 2)
