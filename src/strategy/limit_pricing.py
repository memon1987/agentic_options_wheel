"""Sell-to-open limit pricing, shared by both wheel legs (FC-072).

Before FC-072 the two legs priced sell-to-open orders differently and the
difference was never stated: ``PutSeller`` used ``mid + 10% of spread`` when the
quote was usable, while ``CallSeller`` used a flat ``mid * 0.95`` — 5% below mid
on **every** covered-call write, filled or not. Measured over the journal that
discount cost roughly $2.3k of premium (ceiling) across 138 call fills.

This module is the single implementation both legs now call, so the quote
usability predicate and the stale-quote sanity cap cannot drift apart
(Symmetry Principle, `docs/CLAUDE.md`). The *bias* still differs by leg and that
difference is deliberate: the call leg's bias is configurable
(``strategy.call_limit_spread_fraction``, default ``0.0`` = price at mid), the
put leg keeps its historical ``0.10``.

A quote is **usable** only when all of the following hold:

* ``mid`` is a positive number (the opportunity's ``premium``),
* ``bid`` and ``ask`` are both numbers with ``bid > 0`` and ``ask > bid``,
* ``(ask - bid) / mid <= MAX_SPREAD_TO_MID_RATIO``.

Anything else is a stale, crossed, or missing quote and falls back to
``mid * 0.95`` — the pre-FC-072 call formula, and the put leg's existing
fallback. There is deliberately no third path.
"""

from typing import Any, Optional, Tuple

# A spread wider than half of mid is not a market, it is a stale or crossed
# book. Pricing off its midpoint would be pricing off noise, so such a quote
# takes the conservative fallback. Applies to BOTH legs (FC-072); the put leg
# had no such cap before.
MAX_SPREAD_TO_MID_RATIO = 0.5

# The historical fallback: 5% below mid. Unchanged by FC-072 on either leg.
FALLBACK_MID_DISCOUNT = 0.95


def _as_number(value: Any) -> Optional[float]:
    """Coerce a quote field to float, or None when it is not a real number.

    ``bool`` is excluded explicitly: it is an ``int`` subclass, and a stray
    ``True`` must not read as a $1.00 bid.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def quote_spread(bid: Any, ask: Any) -> Optional[float]:
    """Return ``ask - bid`` when both sides are numbers, else None.

    Reported for telemetry even when the quote is unusable, so a stale or
    crossed book is visible in the logs rather than merely absent.
    """
    bid_f, ask_f = _as_number(bid), _as_number(ask)
    if bid_f is None or ask_f is None:
        return None
    return ask_f - bid_f


def quote_is_usable(mid: Any, bid: Any, ask: Any) -> bool:
    """True when the quote may be used to price a limit off its midpoint."""
    mid_f, bid_f, ask_f = _as_number(mid), _as_number(bid), _as_number(ask)
    if mid_f is None or mid_f <= 0:
        return False
    if bid_f is None or ask_f is None:
        return False
    if bid_f <= 0 or ask_f <= bid_f:
        return False
    return (ask_f - bid_f) / mid_f <= MAX_SPREAD_TO_MID_RATIO


def sell_limit_price(mid: Any, bid: Any, ask: Any,
                     spread_fraction: float) -> Tuple[float, Optional[float], Optional[float]]:
    """Price one sell-to-open limit order.

    Args:
        mid: The opportunity's ``premium`` — the chain midpoint when both sides
            of the book quoted, the last trade otherwise.
        bid: Quote bid, or None/absent.
        ask: Quote ask, or None/absent.
        spread_fraction: Fraction of the bid/ask spread to add to mid on a
            usable quote. ``0.0`` prices exactly at mid.

    Returns:
        ``(limit_price, spread, applied_fraction)``.

        * ``limit_price`` — rounded to the cent. Alpaca accepts penny
          increments on option limits at every price level (FC-072 verified
          against a year of closed orders: 46 sell-call limits at or above
          $3.00 were off the $0.05 tick, 38 of them filled, 0 rejected), so
          there is no tick snapping here.
        * ``spread`` — ``ask - bid`` when both are numbers, else None. For
          telemetry only; a value here does not imply the quote was used.
        * ``applied_fraction`` — ``spread_fraction`` when the quote was usable,
          None when the fallback fired. This is what makes a fallback
          distinguishable from a priced-at-mid write in the logs.
    """
    mid_f = _as_number(mid) or 0.0
    spread = quote_spread(bid, ask)

    if quote_is_usable(mid, bid, ask):
        # spread is not None here: usability required both sides to be numbers.
        return round(mid_f + spread * float(spread_fraction), 2), spread, float(spread_fraction)

    return round(mid_f * FALLBACK_MID_DISCOUNT, 2), spread, None
